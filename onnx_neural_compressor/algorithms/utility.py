# Copyright (c) 2023 MIT HAN Lab
# This source code is licensed under the MIT license
#
# Copyright (c) 2024 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import enum
import os
import re
import struct
import sys
from importlib import util

import numpy as np
from packaging import version

from onnx_neural_compressor import constants
from onnx_neural_compressor import utility

if sys.version_info < (3, 11) and util.find_spec("onnxruntime_extensions"):  # pragma: no cover
    import onnxruntime_extensions

torch = utility.LazyImport("torch")
symbolic_shape_infer = utility.LazyImport("onnxruntime.tools.symbolic_shape_infer")
onnx = utility.LazyImport("onnx")
ort = utility.LazyImport("onnxruntime")


dtype_mapping = {
    "fp32": 1,
    "float32": 1,
    "uint8": 2,
    "int8": 3,
    "uint16": 4,
    "int16": 5,
    "int32": 6,
    "int64": 7,
    "string": 8,
    "bool": 9,
    "fp16": 10,
    "float16": 10,
    "double": 11,
    "uint32": 12,
    "uint64": 13,
    "complex64": 14,
    "complex128": 15,
    "bf16": 16,
    "bfloat16": 16,
}

QUANT_OP_NAME_SUFFIX = "_quant"
__producer__ = "onnx.quantize"
__version__ = "0.1.0"
onnx_domain = "ai.onnx"
ms_domain = "com.microsoft"

ONNX_INT_TYPE_RANGE = {
    onnx.TensorProto.UINT8: (0, 255),
    onnx.TensorProto.INT8: (-128, 127),
}

ONNX_INT_TYPE_SYMMETRIC_RANGE = {
    onnx.TensorProto.INT8: (-127, 127),
}

ONNX_INT_TYPE_REDUCED_RANGE = {
    onnx.TensorProto.UINT8: (0, 127),
    onnx.TensorProto.INT8: (-64, 64),
}

def is_quantizable_type(data_type):
    return data_type in [onnx.TensorProto.FLOAT, onnx.TensorProto.FLOAT16, onnx.TensorProto.BFLOAT16]

def get_qmin_qmax_for_qType(qType, reduce_range=False, sym=False):  # noqa: N802
    """Get qmin, qmax for qType."""
    if qType == onnx.TensorProto.FLOAT8E4M3FN:
        raise NotImplementedError("This function is not implemented for float 8 as not needed.")

    qrange = None

    if reduce_range:
        qrange = ONNX_INT_TYPE_REDUCED_RANGE.get(qType)
    elif sym and qType in ONNX_INT_TYPE_SYMMETRIC_RANGE:
        qrange = ONNX_INT_TYPE_SYMMETRIC_RANGE[qType]
    else:
        qrange = ONNX_INT_TYPE_RANGE.get(qType)

    if not qrange:
        raise ValueError(f"Unexpected data type {qType} requested. Only INT8 and UINT8 are supported.")

    return qrange

def dtype_to_name(dtype_mapping, dtype):
    """Map data type and its string representation."""
    return list(dtype_mapping.keys())[list(dtype_mapping.values()).index(dtype)]


def _get_blob_size(group_size, has_zp):  # pragma: no cover
    """Get blob_size.

    Args:
        group_size (int): how many elements share one scale/zp
        has_zp (bool): whether zero_point is None
    """
    if version.Version(ort.__version__) > constants.ONNXRT1161_VERSION:
        blob_size = group_size // 2
    elif has_zp:
        blob_size = group_size // 2 + 4 + 1
    else:
        blob_size = group_size // 2 + 4
    return blob_size


def make_matmul_weight_only_node(
    node: onnx.NodeProto,
    weight_shape: tuple,
    num_bits: int,
    group_size: int,
    k_blocks: int,
    q_weight: np.array,
    scale: np.array,
    zero_point: np.array,
    accuracy_level: int = 0,
):
    """Build MatMulFpQ4/MatMulNBits node.

    Args:
        node (onnx.NodeProto): original matmul node
        weight_shape (tuple): original weight shape
        num_bits (int): number of bits used to represent weights.
        group_size (int): how many elements share one scale/zp
        k_blocks (int): block number
        q_weight (np.array): quantized weight
        scale (np.array): scale
        zero_point (np.array): zero point
        accuracy_level (int, optional): accuracy level.
            Support 0 (unset), 1(fp32 compute type of jblas kernel),
            2 (fp16 compute type of jblas kernel), 3 (bf16 compute type of jblas kernel),
            4 (int8 compute type of jblas kernel) Defaults to 0.

    Returns:
        matmul_weight_only_node: MatMulFpQ4 or MatMulNBits node
        new_inits: initializers of the new node
    """
    blob_size = _get_blob_size(group_size, zero_point is not None)
    packed = np.zeros((q_weight.shape[0], blob_size), dtype="uint8")
    q_weight_name = node.input[1] + "_Q{}G{}".format(str(num_bits), str(group_size))
    input_names = [node.input[0], q_weight_name]
    new_inits = []
    kwargs = {}

    if version.Version(ort.__version__) > constants.ONNXRT1161_VERSION:
        op_type = "MatMulNBits"

        # pack quantized weight
        for i in range(q_weight.shape[0]):
            for k in range(0, group_size, 2):
                packed[i][k // 2] = q_weight[i][k] | q_weight[i][k + 1] << 4
        packed = np.reshape(packed, (-1, k_blocks, blob_size))

        # build scale tensor
        scale = np.reshape(scale, (-1, k_blocks))
        scale_tensor = onnx.helper.make_tensor(
            name=node.input[1] + "_scale",
            data_type=dtype_mapping[str(scale.dtype)],
            dims=scale.shape,
            vals=scale.tobytes(),
            raw=True,
        )
        input_names.append(scale_tensor.name)
        new_inits.append(scale_tensor)

        # build zero_point tensor
        if zero_point is not None:
            if num_bits > 4:
                packed_zp = np.reshape(zero_point, (1, -1)).astype("uint8")
            else:
                packed_zp = np.full((zero_point.shape[0] + 1) // 2, 136, dtype="uint8")
                for i in range(zero_point.shape[0] // k_blocks):
                    for j in range(k_blocks):
                        idx = i * k_blocks + j
                        zp = zero_point[idx]
                        packed_zp[idx // 2] = (
                            ((packed_zp[idx // 2] & 0x0F) | (zp << 4))
                            if (idx & 1)
                            else ((packed_zp[idx // 2] & 0xF0) | zp)
                        )

            zp_tensor = onnx.helper.make_tensor(
                name=node.input[1] + "_zp", data_type=2, dims=packed_zp.shape, vals=packed_zp.tobytes(), raw=True
            )
            input_names.append(zp_tensor.name)
            new_inits.append(zp_tensor)

        # set kwargs
        kwargs["K"] = weight_shape[0]
        kwargs["N"] = weight_shape[1]
        kwargs["bits"] = num_bits
        kwargs["block_size"] = group_size
        if accuracy_level > 0:
            # require onnxruntime > 1.16.3
            kwargs["accuracy_level"] = accuracy_level

    else:
        offset = 5 if zero_point is not None else 4
        op_type = "MatMulFpQ4"

        # pack quantized weight
        for i in range(q_weight.shape[0]):
            bf = struct.pack("f", scale[i])
            packed[i][0] = bf[0]
            packed[i][1] = bf[1]
            packed[i][2] = bf[2]
            packed[i][3] = bf[3]

            if zero_point is not None:
                packed[i][4] = zero_point[i]

            packed[i][offset:] = np.bitwise_or(
                q_weight[i][: group_size // 2], np.left_shift(q_weight[i][group_size // 2 :], num_bits)
            )
        packed = packed.reshape(-1)

        # build shape tensor
        shape_tensor = onnx.helper.make_tensor(
            name=node.input[1] + "_shape", data_type=7, dims=(2,), vals=np.array(weight_shape, dtype="int64")
        )
        new_inits.append(shape_tensor)
        input_names.append(shape_tensor.name)

        # set kwargs
        kwargs["blk_quant_type"] = 1 if zero_point is not None else 0

    q_weight_tensor = onnx.helper.make_tensor(
        name=q_weight_name,
        data_type=2,
        dims=packed.shape,
        vals=packed.tobytes(),
        raw=True,
    )
    new_inits.append(q_weight_tensor)

    matmul_weight_only_node = onnx.helper.make_node(
        op_type,
        inputs=input_names,
        outputs=node.output,
        name=node.name + "_Q" + str(num_bits) if node.name else "_Q" + str(num_bits),
        domain="com.microsoft",
        **kwargs,
    )
    return matmul_weight_only_node, new_inits


def prepare_inputs(model, data_reader, providers):
    """Prepare inputs for weight only quantization.

    Args:
        model (ModelProto or onnx_model.ONNXModel): onnx model.
        data_reader (CalibrationDataReader): a calibration data reader.
        providers (list): providers to use.

    Returns:
        inputs: prepared inputs.
        so: session options
    """

    so = ort.SessionOptions()
    if sys.version_info < (3, 11) and util.find_spec("onnxruntime_extensions"):  # pragma: no cover
        so.register_custom_ops_library(onnxruntime_extensions.get_library_path())
    if model.is_large_model:
        onnx.save_model(
            model.model,
            model.model_path + "_augment.onnx",
            save_as_external_data=True,
            all_tensors_to_one_file=True,
            convert_attribute=False,
        )

    inputs_list = []
    while True:
        inputs = data_reader.get_next()
        if not inputs:
            break
        inputs_list.append(inputs)
    return inputs_list, so


def pad_tensor(weight, group_size, k_blocks):
    """Pad tensor rowi so that it can be is divisible by group_size.

    Args:
        weight (array): weight
        group_size (int): how many elements share one scale/zp
        k_blocks (int): the number of block

    Returns:
        weight: paded weight
    """
    if group_size == -1:
        return weight

    org_w_shape = weight.shape
    padded_rows = k_blocks * group_size
    pad_len = padded_rows - org_w_shape[0]

    if pad_len > 0:
        weight = np.pad(weight, ((0, pad_len), (0, 0)), "constant")

    return weight


def quant_tensor(
    data: np.array,
    num_bits: int = 4,
    group_size: int = 32,
    sym: bool = False,
    dtype: str = "int",
    ratio: float = 1.0,
):
    """Quantize tensor per group.

    Args:
        data (np.array): input weight
        num_bits (int, optional): number of bits used to represent weights. Defaults to 4.
        group_size (int, optional): how many elements share one scale/zp. Defaults to 4.
        sym (bool, optional): _quantization scheme. Defaults to False.
        dtype (str, optional): data type. Defaults to "int".
        ratio (float, optional): percentile of clip. Defaults to 1.0.

    Returns:
        output: quantized weight
        scale: scale
        zero_point: zero point
    """
    data = np.reshape(data, (-1, group_size))
    if not sym or dtype == "uint":
        maxq = 2**num_bits - 1
        minq = 0
    elif sym:
        maxq = 2 ** (num_bits - 1) - 1 if num_bits != 1 else 0
        minq = -(2 ** (num_bits - 1)) if num_bits != 1 else -1

    rmin = np.min(data, axis=1, keepdims=True) * ratio
    rmax = np.max(data, axis=1, keepdims=True) * ratio
    if sym:
        max_range = np.maximum(np.abs(rmin), np.abs(rmax))
        scale = np.ones(rmax.shape)
        scale[max_range > 0] = np.array(
            [float(i) / (maxq - minq) for i in (max_range[max_range > 0] * 2.0).flatten().tolist()]
        )
        zero_point = (
            np.zeros(scale.shape) if dtype == "int" else np.ones(rmax.shape, dtype="uint8") * (1 << (num_bits - 1))
        )
    else:
        scale = np.ones(rmax.shape)
        scale[rmin != rmax] = np.array(
            [float(i) / (maxq - minq) for i in (rmax - rmin)[rmin != rmax].flatten().tolist()]
        )
        zero_point = (
            ((np.zeros(scale.shape) - rmin) / scale).round()
            if dtype == "int"
            else np.maximum(0, np.minimum(maxq, ((np.zeros(scale.shape) - rmin) / scale).round())).astype("uint8")
        )
    return np.clip((data / scale + zero_point).round(), minq, maxq), scale, zero_point


def qdq_tensor(
    data: np.array,
    num_bits: int = 4,
    group_size: int = 32,
    sym: bool = False,
    dtype: str = "int",
    ratio: float = 1.0,
):
    """Quant dequant tensor per group.

    Args:
        data (np.array): input weight
        num_bits (int, optional): number of bits used to represent weights. Defaults to 4.
        group_size (int, optional):  how many elements share one scale/zp. Defaults to 32.
        sym (bool, optional): quantization scheme. Defaults to False.
        dtype (str, optional): data type. Defaults to "int".
        ratio (float, optional): percentile of clip. Defaults to 1.0.

    Returns:
        output: quant-dequant weight
    """
    org_shape = data.shape
    weight, scale, zp = quant_tensor(data, num_bits, group_size, sym, dtype, ratio)
    return np.reshape(scale * (weight - zp), org_shape)


def is_B_transposed(node):
    """Whether inuput B is transposed."""
    transB = [attr for attr in node.attribute if attr.name == "transB"]
    if len(transB):
        return 0 < onnx.helper.get_attribute_value(transB[0])
    return False


def calculate_scale_zp(rmin, rmax, quantize_range, qType, sym):
    """Calculate scale and zero point."""
    qmin, qmax = quantize_range
    dtype = onnx.helper.tensor_dtype_to_np_dtype(qType)
    if isinstance(rmax, np.ndarray):
        if sym:
            max_range = np.maximum(abs(rmin), abs(rmax))
            rmin = - max_range
            rmax = max_range
        scale = (rmax - rmin) / (qmax - qmin)
        scale[scale < np.finfo(rmax.dtype).tiny] = 1
        zero_point = np.multiply(np.ones(rmax.shape), np.round((qmax + qmin) / 2.0)).astype(dtype) if sym else \
            np.round(qmin - rmin / scale).astype(dtype)
    else:
        if sym:
            max_range = max(abs(rmin), abs(rmax))
            scale = (float(max_range) * 2) / (qmax - qmin) if max_range > 0 else 1
        else:
            scale = (float(rmax) - float(rmin)) / (qmax - qmin) if rmin != rmax else 1
        zero_point = np.round((qmax + qmin) / 2.0).astype(dtype) if sym else \
            np.round(qmin - rmin / scale).astype(dtype)
    return np.float32(scale), zero_point

def quantize_data(data, quantize_range, qType, sym):
    """Quantize data.

    To pack weights, we compute a linear transformation
        - when data type == uint8 mode, from [rmin, rmax] -> [0, 2^{b-1}] and
        - when data type == int8, from [-m , m] -> [-(2^{b-1}-1), 2^{b-1}-1] where
            m = max(abs(rmin), abs(rmax))
    and add necessary intermediate nodes to transform quantized weight to full weight
    using the equation r = S(q-z), where
        r: real original value
        q: quantized value
        S: scale
        z: zero point

    Args:
        data (array): data to quantize
        quantize_range (list): list of data to weight pack.
        qType (int): data type to quantize to. Supported types UINT8 and INT8
        sym (bool): whether use sym quantization.
    """
    rmin = np.min(np.min(data), 0)
    rmax = np.max(np.max(data), 0)

    scale, zero_point = calculate_scale_zp(rmin, rmax, quantize_range, qType, sym)
    quantized_data = quantize_nparray(qType, data, scale, zero_point, low=quantize_range[0], high=quantize_range[1])
    return rmin, rmax, zero_point, scale, quantized_data


def get_node_original_name(node) -> str:
    """Get the original name of the given node."""
    node_name: str = node.name
    # TODO how to handle the unquantized node that has the `_quant` suffix, such as `conv_quant`?
    if node_name.endswith(QUANT_OP_NAME_SUFFIX):
        return node_name[: -len(QUANT_OP_NAME_SUFFIX)]
    else:
        # For unquantized nodes
        return node_name

class QuantType(enum.Enum):  # pragma: no cover
    """Represent QuantType value."""

    QInt8 = 0
    QUInt8 = 1


def split_shared_bias(model):
    """Split shared tensor."""
    for input_name, node_list in model.input_name_to_nodes.items():
        if len(node_list) > 1 and input_name in [i.name for i in model.model.graph.initializer]:
            for node in node_list[1:]:
                if node.op_type not in ["Conv", "FusedConv"]:
                    continue
                if len(node.input) > 2 and node.input[2] == input_name:
                    new_input_name = node.input[2] + "_nc_split_" + node.name
                    new_input = onnx.helper.make_tensor(
                        new_input_name,
                        model.get_initializer(input_name).data_type,
                        model.get_initializer(input_name).dims,
                        model.get_initializer(input_name).raw_data,
                        True,
                    )
                    model.add_initializer(new_input)
                    node.input[2] = new_input_name
    return model


def remove_init_from_model_input(model):
    """Remove initializer from model input."""
    inputs = model.model.graph.input
    name_to_input = {}
    for inp in inputs:
        name_to_input[inp.name] = inp
    for initializer in model.model.graph.initializer:
        if initializer.name in name_to_input:
            inputs.remove(name_to_input[initializer.name])


def quantize_data_per_channel(data, axis, quantize_range, qType, sym):
    """Quantize tensor per-channel."""
    rmin = None
    rmax = None
    for i in range(len(data.shape)):
        if i != axis:
            rmin = np.min(data, axis=i, keepdims=True) if rmin is None else np.min(rmin, axis=i, keepdims=True)
            rmax = np.max(data, axis=i, keepdims=True) if rmax is None else np.max(rmax, axis=i, keepdims=True)
    rmin = np.minimum(rmin, 0)
    rmax = np.maximum(rmax, 0)
    scale, zero_point = calculate_scale_zp(rmin, rmax, quantize_range, qType, sym)
    quantized_data = quantize_nparray(qType, data, scale, zero_point, low=quantize_range[0], high=quantize_range[1])
    return rmin.reshape(-1, 1), rmax.reshape(-1, 1), zero_point.reshape(-1, 1), scale.reshape(-1, 1), quantized_data


def dequantize_data_with_scale_zero(tensor_value, scale_value, zo_value):  # pragma: no cover
    """Dequantize tensor with scale and zero point."""
    return (tensor_value.astype(scale_value.dtype) - zo_value.astype(scale_value.dtype)) * scale_value


def dequantize_data(tensor_value, scale_value, zo_value, axis=0):  # pragma: no cover
    """Dequantize tensor."""
    if not isinstance(scale_value, np.ndarray):
        return dequantize_data_with_scale_zero(tensor_value, scale_value, zo_value)
    else:
        channel_count = tensor_value.shape[axis]  # TBD, default from axis 0
        new_per_channel_tensor_values = []
        for i in range(channel_count):
            per_channel_tensor_value = tensor_value.take(i, axis)
            per_channel_scale_value = scale_value.take(i)
            per_channel_zero_value = zo_value.take(i)
            new_per_channel_tensor_values.append(
                dequantize_data_with_scale_zero(
                    per_channel_tensor_value, per_channel_scale_value, per_channel_zero_value
                )
            )
        # combine per_channel_data into one
        reshape_dims = list(tensor_value.shape)  # deep copy
        reshape_dims[axis] = 1  # only one per channel for reshape
        new_tensor_value = new_per_channel_tensor_values[0].reshape(reshape_dims)
        for i in range(1, channel_count):
            new_per_channel_tensor_value = new_per_channel_tensor_values[i].reshape(reshape_dims)
            new_tensor_value = np.concatenate((new_tensor_value, new_per_channel_tensor_value), axis)
        return new_tensor_value


class ValueInfo:  # pragma: no cover
    """Represents a casted tensor info."""

    def __init__(self, tensor_name, dtype, new_dtype):
        """Initialization.

        Args:
            tensor_name (string): tensor name
            dtype (int): original data type
            new_dtype (int): target data type
        """
        self.tensor_name = tensor_name
        self.dtype = dtype
        self.new_dtype = new_dtype


class QuantizedValue:
    """Represents a linearly quantized value (input/output/initializer)."""

    def __init__(
        self,
        name,
        new_quantized_name,
        scale_name,
        zero_point_name,
        quantized_value_type,
        axis=None,
        qType=QuantType.QUInt8,
    ):
        """Initialization.

        Args:
            name (string): tensor name
            new_quantized_name (string): quantized tensor name
            scale_name (string): scale name
            zero_point_name (string): zero point name
            quantized_value_type (QuantizedValueType): quantized value type
            axis (int, optional): quantized axis. Defaults to None.
            qType (int, optional): quantized data type. Defaults to QuantType.QUInt8.
        """
        self.name = name
        self.q_name = new_quantized_name
        self.scale_name = scale_name
        self.zp_name = zero_point_name
        self.value_type = quantized_value_type
        self.axis = axis
        self.qType = qType


class QuantizedInitializer:
    """Represents a linearly quantized weight input from ONNX operators."""

    def __init__(
        self,
        name,
        initializer,
        rmins,
        rmaxs,
        zero_points,
        scales,
        data=[],
        quantized_data=[],
        axis=None,
        qType=QuantType.QUInt8,
    ):
        """Initialization.

        Args:
            name (string): initializer name
            initializer (onnx.onnx_ml_pb2.TensorProto): initializer
            rmins (list): list of min value
            rmaxs (list): list of max value
            zero_points (list): list of zero point
            scales (list): list of scale
            data (list, optional): array version of the initializer. Defaults to [].
            quantized_data (list, optional): quantized data. Defaults to [].
            axis (int, optional): quantized axis. Defaults to None.
            qType (int, optional): quantized data type. Defaults to QuantType.QUInt8.
        """
        self.name = name
        self.initializer = initializer  # TensorProto initializer in ONNX graph
        self.rmins = rmins  # List of minimum range for each axis
        self.rmaxs = rmaxs  # List of maximum range for each axis
        # 1D tensor of zero points computed for each axis. scalar if axis is empty
        self.zero_points = zero_points
        self.scales = scales  # 1D tensor of scales computed for each axis. scalar if axis is empty
        self.data = data  # original data from initializer TensorProto
        self.quantized_data = quantized_data  # weight-packed data from data
        # Scalar to specify which dimension in the initializer to weight pack.
        self.axis = axis
        # If empty, single zero point and scales computed from a single rmin and rmax
        self.qType = qType


class QuantizedValueType(enum.Enum):  # pragma: no cover
    """Represent QuantizedValueType value."""

    Input = 0
    Initializer = 1


def quantize_nparray(qtype, arr, scale, zero_point, low=None, high=None):
    """Quantize numpy array."""
    dtype = onnx.helper.tensor_dtype_to_np_dtype(qtype)
    arr_fp32 = np.asarray((np.asarray(arr).astype(np.float32) / scale).round() + zero_point)
    if low is not None and high is not None:
        np.clip(arr_fp32, low, high, out=arr_fp32)
    return arr_fp32.astype(dtype)


def attribute_to_kwarg(attribute):
    """Convert attribute to kwarg format for use with onnx.helper.make_node."""
    attribute_mapping = {
        1: attribute.f,
        2: attribute.i,
        3: attribute.s,
        4: attribute.t,
        5: attribute.g,
        6: attribute.floats,
        7: attribute.ints,
        8: attribute.strings,
        9: attribute.tensors,
        10: attribute.graphs,
    }
    if attribute.type in attribute_mapping:
        value = attribute_mapping[attribute.type]
    else:  # pragma: no cover
        raise ValueError(
            "attribute {} has no type specified " "or unsupported type {}.".format(attribute.name, attribute.type)
        )
    return {attribute.name: value}


def trt_env_setup(model):
    """Set environment variable for Tensorrt Execution Provider."""
    is_int8 = False
    for node in model.graph.node:
        if node.op_type in ["QuantizeLinear", "DequantizeLinear"]:
            is_int8 = True
            break
    if is_int8:
        os.environ["ORT_TENSORRT_INT8_ENABLE"] = "1"
    else:
        os.environ["ORT_TENSORRT_INT8_ENABLE"] = "0"


def infer_shapes(in_mp, int_max=2**31 - 1, auto_merge=False, guess_output_rank=False, verbose=0, base_dir=""):
    """Symbolic shape inference."""

    class SymbolicShapeInference(symbolic_shape_infer.SymbolicShapeInference):
        def __init__(self, int_max, auto_merge, guess_output_rank, verbose, prefix="", base_dir=""):
            super().__init__(int_max, auto_merge, guess_output_rank, verbose, prefix)
            self.base_dir = base_dir

        def _get_value(self, node, idx):
            name = node.input[idx]
            assert name in self.sympy_data_ or name in self.initializers_
            return (
                self.sympy_data_[name]
                if name in self.sympy_data_
                else onnx.numpy_helper.to_array(self.initializers_[name], base_dir=self.base_dir)
            )

    onnx_opset = symbolic_shape_infer.get_opset(in_mp)
    if (not onnx_opset) or onnx_opset < 7:
        logger.warning("Only support models of onnx opset 7 and above.")
        return None
    symbolic_shape_inference = SymbolicShapeInference(
        int_max, auto_merge, guess_output_rank, verbose, base_dir=base_dir
    )
    all_shapes_inferred = False
    symbolic_shape_inference._preprocess(in_mp)
    while symbolic_shape_inference.run_:
        all_shapes_inferred = symbolic_shape_inference._infer_impl()
    symbolic_shape_inference._update_output_from_vi()
    if not all_shapes_inferred:
        onnx.save_model(symbolic_shape_inference.out_mp_, "sym_shape_infer_temp.onnx", save_as_external_data=True)
        raise Exception("Incomplete symbolic shape inference")
    return symbolic_shape_inference.out_mp_

def dump_model_op_stats(model, quantize_config, fp32_op_list):
    qdq_ops = ["QuantizeLinear", "DequantizeLinear", "DynamicQuantizeLinear"]
    res = {}
    for op_type in fp32_op_list:
        res[op_type] = {"INT8": 0, "FP32": 0}
    for op_type in qdq_ops:
        res[op_type] = {"INT8": 0, "FP32": 0}

    for node in model.graph.node:
        if node.name.endswith("_quant"):
            if node.op_type.startswith("QLinear"):
                origin_op_type = node.op_type.split("QLinear")[-1]
            else:
                origin_op_type = node.op_type.split("Integer")[0]

            if origin_op_type in ["QAttention", "QGemm"]:
                origin_op_type = origin_op_type[1:]
            elif origin_op_type == "DynamicQuantizeLSTM":
                origin_op_type = "LSTM"
            elif origin_op_type == "QEmbedLayerNormalization":
                origin_op_type = "EmbedLayerNormalization"
            res[origin_op_type]["INT8"] += 1

        elif node.op_type in qdq_ops:
            res[node.op_type]["INT8"] += 1

        elif node.op_type in res:
            res[node.op_type]["FP32"] += 1

    field_names = ["Op Type", "Total", "INT8", "FP32"]
    output_data = [
        [
            op_type,
            sum(res[op_type].values()),
            res[op_type]["INT8"],
            res[op_type]["FP32"],
        ]
        for op_type in res.keys()
    ]

    utility.Statistics(output_data, header="Quantization Statistics", field_names=field_names).print_stat()

def dump_woq_stats(model, quantize_config, fp32_op_list):
    res = {}
    for optype in fp32_op_list:
        res[optype] = {}

    dtype_set = set()
    for node in model.graph.node:
        if node.op_type in ["MatMulFpQ4", "MatMulNBits"]:
            optype = "MatMul"
        else:
            optype = node.op_type

        if optype not in res:
            continue
        if re.fullmatch("^.*_Q\d*G\d*", node.input[1]):
            search_out = re.search("_Q\d*", node.input[1])
            dtype = "A32W{}G{}".format(
                node.input[1][search_out.start() + 2 : search_out.end()], node.input[1][search_out.end() + 1 :]
            )
        else:
            dtype = "FP32"
        dtype_set.add(dtype)

        if dtype in res[optype]:
            res[optype][dtype] += 1
        else:
            res[optype][dtype] = 1

    dtype_list = list(dtype_set)
    for dtype in dtype_list:
        for optype in res.keys():
            if dtype not in res[optype]:
                res[optype][dtype] = 0

    # update stats format for dump.
    field_names = ["Op Type", "Total"]
    field_names.extend(dtype_list)
    output_data = []
    for op_type in res.keys():
        field_results = [op_type, sum(res[op_type].values())]
        field_results.extend([res[op_type][dtype] for dtype in dtype_list])
        output_data.append(field_results)

    utility.Statistics(output_data, header="Mixed Precision Statistics", field_names=field_names).print_stat()
