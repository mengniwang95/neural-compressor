# Copyright (c) 2023 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib
import logging
import os
import pathlib
import subprocess
import time

import cpuinfo
import numpy as np
import onnx
import onnxruntime as ort
import prettytable as pt
import psutil
from onnx_neural_compressor import constants
from onnx_neural_compressor import logger
from onnxruntime import quantization
from onnxruntime.quantization import onnx_model

from typing import Callable, Dict, List, Tuple, Union  # isort: skip

# Dictionary to store a mapping between algorithm names and corresponding algo implementation(function)
algos_mapping: Dict[str, Callable] = {}


#######################################################
####   Options
#######################################################


def check_value(name, src, supported_type, supported_value=[]):
    """Check if the given object is the given supported type and in the given supported value.

    Example::

        from onnx_neural_compressor import utility

        def datatype(self, datatype):
            if utility.check_value("datatype", datatype, list, ["fp32", "bf16", "uint8", "int8"]):
                self._datatype = datatype
    """
    if isinstance(src, list) and any([not isinstance(i, supported_type) for i in src]):
        assert False, "Type of {} items should be {} but not {}".format(
            name, str(supported_type), [type(i) for i in src]
        )
    elif not isinstance(src, list) and not isinstance(src, supported_type):
        assert False, "Type of {} should be {} but not {}".format(name, str(supported_type), type(src))

    if len(supported_value) > 0:
        if isinstance(src, str) and src not in supported_value:
            assert False, "{} is not in supported {}: {}. Skip setting it.".format(src, name, str(supported_value))
        elif (
            isinstance(src, list)
            and all([isinstance(i, str) for i in src])
            and any([i not in supported_value for i in src])
        ):
            assert False, "{} is not in supported {}: {}. Skip setting it.".format(src, name, str(supported_value))

    return True


class Options:
    """Option Class for configs.

    This class is used for configuring global variables. The global variable options is created with this class.
    If you want to change global variables, you should use functions from onnx_neural_compressor.utility.py:
        set_random_seed(seed: int)
        set_workspace(workspace: str)
        set_resume_from(resume_from: str)

    Args:
        random_seed(int): Random seed used in neural compressor.
                          Default value is 1978.
        workspace(str): The directory where intermediate files and tuning history file are stored.
                        Default value is:
                            "./nc_workspace/{}/".format(datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")).
        resume_from(str): The directory you want to resume tuning history file from.
                          The tuning history was automatically saved in the workspace directory
                               during the last tune process.
                          Default value is None.

    Example::

        from onnx_neural_compressor import set_random_seed
        from onnx_neural_compressor import set_workspace
        from onnx_neural_compressor import set_resume_from
        set_random_seed(2022)
        set_workspace("workspace_path")
        set_resume_from("workspace_path")
    """

    def __init__(self, random_seed=1978, workspace=constants.DEFAULT_WORKSPACE, resume_from=None):
        """Init an Option object."""
        self.random_seed = random_seed
        self.workspace = workspace
        self.resume_from = resume_from

    @property
    def random_seed(self):
        """Get random seed."""
        return self._random_seed

    @random_seed.setter
    def random_seed(self, random_seed):
        """Set random seed."""
        if check_value("random_seed", random_seed, int):
            self._random_seed = random_seed

    @property
    def workspace(self):
        """Get workspace."""
        return self._workspace

    @workspace.setter
    def workspace(self, workspace):
        """Set workspace."""
        if check_value("workspace", workspace, str):
            self._workspace = workspace

    @property
    def resume_from(self):
        """Get resume_from."""
        return self._resume_from

    @resume_from.setter
    def resume_from(self, resume_from):
        """Set resume_from."""
        if resume_from is None or check_value("resume_from", resume_from, str):
            self._resume_from = resume_from


options = Options()

def singleton(cls):
    """Singleton decorator."""

    instances = {}

    def _singleton(*args, **kw):
        """Create a singleton object."""
        if cls not in instances:
            instances[cls] = cls(*args, **kw)
        return instances[cls]

    return _singleton


class Statistics:
    """The statistics printer."""

    def __init__(self, data, header, field_names, output_handle=logger.info):
        """Init a Statistics object.

        Args:
            data: The statistics data
            header: The table header
            field_names: The field names
            output_handle: The output logging method
        """
        self.field_names = field_names
        self.header = header
        self.data = data
        self.output_handle = output_handle
        self.tb = pt.PrettyTable(min_table_width=40)

    def print_stat(self):
        """Print the statistics."""
        valid_field_names = []
        for index, value in enumerate(self.field_names):
            if index < 2:
                valid_field_names.append(value)
                continue

            if any(i[index] for i in self.data):
                valid_field_names.append(value)
        self.tb.field_names = valid_field_names
        for i in self.data:
            tmp_data = []
            for index, value in enumerate(i):
                if self.field_names[index] in valid_field_names:
                    tmp_data.append(value)
            if any(tmp_data[1:]):
                self.tb.add_row(tmp_data)
        lines = self.tb.get_string().split("\n")
        self.output_handle("|" + self.header.center(len(lines[0]) - 2, "*") + "|")
        for i in lines:
            self.output_handle(i)


class LazyImport(object):
    """Lazy import python module till use."""

    def __init__(self, module_name):
        """Init LazyImport object.

        Args:
           module_name (string): The name of module imported later
        """
        self.module_name = module_name
        self.module = None

    def __getattr__(self, name):
        """Get the attributes of the module by name."""
        try:
            self.module = importlib.import_module(self.module_name)
            mod = getattr(self.module, name)
        except:
            spec = importlib.util.find_spec(str(self.module_name + "." + name))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        return mod

    def __call__(self, *args, **kwargs):
        """Call the function in that module."""
        function_name = self.module_name.split(".")[-1]
        module_name = self.module_name.split(f".{function_name}")[0]
        self.module = importlib.import_module(module_name)
        function = getattr(self.module, function_name)
        return function(*args, **kwargs)


@singleton
class CpuInfo(object):
    """CPU info collection."""

    def __init__(self):
        """Get whether the cpu numerical format is bf16, the number of sockets, cores and cores per socket."""
        self._bf16 = False
        self._vnni = False
        info = cpuinfo.get_cpu_info()
        if "arch" in info and "X86" in info["arch"]:
            cpuid = cpuinfo.CPUID()
            max_extension_support = cpuid.get_max_extension_support()
            if max_extension_support >= 7:
                ecx = cpuid._run_asm(
                    b"\x31\xC9",  # xor ecx, ecx
                    b"\xB8\x07\x00\x00\x00" b"\x0f\xa2" b"\x89\xC8" b"\xC3",  # mov eax, 7  # cpuid  # mov ax, cx  # ret
                )
                self._vnni = bool(ecx & (1 << 11))
                eax = cpuid._run_asm(
                    b"\xB9\x01\x00\x00\x00",  # mov ecx, 1
                    b"\xB8\x07\x00\x00\x00" b"\x0f\xa2" b"\xC3",  # mov eax, 7  # cpuid  # ret
                )
                self._bf16 = bool(eax & (1 << 5))
        # TODO: The implementation will be refined in the future.
        # https://github.com/intel/neural-compressor/tree/detect_sockets
        if "arch" in info and "ARM" in info["arch"]:  # pragma: no cover
            self._sockets = 1
        else:
            self._sockets = self.get_number_of_sockets()
        self._cores = psutil.cpu_count(logical=False)
        self._cores_per_socket = int(self._cores / self._sockets)

    @property
    def bf16(self):
        """Get whether it is bf16."""
        return self._bf16

    @property
    def vnni(self):
        """Get whether it is vnni."""
        return self._vnni

    @property
    def cores_per_socket(self):
        """Get the cores per socket."""
        return self._cores_per_socket

    def get_number_of_sockets(self) -> int:
        """Get number of sockets in platform."""
        cmd = "cat /proc/cpuinfo | grep 'physical id' | sort -u | wc -l"
        if psutil.WINDOWS:
            cmd = r'wmic cpu get DeviceID | C:\Windows\System32\find.exe /C "CPU"'
        elif psutil.MACOS:  # pragma: no cover
            cmd = "sysctl -n machdep.cpu.core_count"

        with subprocess.Popen(
            args=cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=False,
        ) as proc:
            proc.wait()
            if proc.stdout:
                for line in proc.stdout:
                    return int(line.decode("utf-8", errors="ignore").strip())
        return 0


def dump_elapsed_time(customized_msg=""):
    """Get the elapsed time for decorated functions.

    Args:
        customized_msg (string, optional): The parameter passed to decorator. Defaults to None.
    """

    def f(func):

        def fi(*args, **kwargs):
            start = time.time()
            res = func(*args, **kwargs)
            end = time.time()
            logger.info(
                "%s elapsed time: %s ms"
                % (customized_msg if customized_msg else func.__qualname__, round((end - start) * 1000, 2))
            )
            return res

        return fi

    return f


def set_random_seed(seed: int):
    """Set the random seed in config."""
    options.random_seed = seed


def set_workspace(workspace: str):
    """Set the workspace in config."""
    options.workspace = workspace


def set_resume_from(resume_from: str):
    """Set the resume_from in config."""
    options.resume_from = resume_from


def find_by_name(name, item_list):
    """Helper function to find item by name in a list."""
    items = []
    for item in item_list:
        assert hasattr(item, "name"), "{} should have a 'name' attribute defined".format(item)  # pragma: no cover
        if item.name == name:
            items.append(item)
    if len(items) > 0:
        return items[0]
    else:
        return None


def simple_progress_bar(total, i):
    """Progress bar for cases where tqdm can't be used."""
    progress = i / total
    bar_length = 20
    bar = "#" * int(bar_length * progress)
    spaces = " " * (bar_length - len(bar))
    percentage = progress * 100
    print(f"\rProgress: [{bar}{spaces}] {percentage:.2f}%", end="")


def register_algo(name):
    """Decorator function to register algorithms in the algos_mapping dictionary.

    Usage example:
        @register_algo(name=example_algo)
        def example_algo(model: Union[onnx.ModelProto, pathlib.Path, str],
                         quant_config: RTNConfig) -> onnx.ModelProto:
            ...

    Args:
        name (str): The name under which the algorithm function will be registered.

    Returns:
        decorator: The decorator function to be used with algorithm functions.
    """

    def decorator(algo_func):
        algos_mapping[name] = algo_func
        return algo_func

    return decorator

def check_model_with_infer_shapes(model):
    """Check if the model has been shape inferred."""
    if isinstance(model, (pathlib.Path, str)):
        model = onnx.load(model, load_external_data=False)
    elif isinstance(model, onnx_model.ONNXModel):
        model = model.model
    if len(model.graph.value_info) > 0:
        return True
    return False

def auto_detect_ep():
    eps = ort.get_available_providers()
    if "DnnlExecutionProvider" in eps:
        return "DnnlExecutionProvider"
    elif "DmlExecutionProvider" in eps:
        return "DnnlExecutionProvider"
    elif "CUDAExecutionProvider" in eps:
        return "CUDAExecutionProvider"
    else:
        return "CPUExecutionProvider"

def static_basic_check(config, optype, execution_provider, quant_format):
    if quant_format == quantization.QuantFormat.QOperator:
        if execution_provider not in constants.STATIC_QOPERATOR_OP_LIST_MAP:
            raise ValueError("Unsupported execution_provider {}, only support {}.".format(execution_provider, list(constants.STATIC_QOPERATOR_OP_LIST_MAP.keys())))
        supported_optype = constants.STATIC_QOPERATOR_OP_LIST_MAP[execution_provider]
        if optype not in supported_optype:
            raise ValueError("Unsupported optype {} for {}, only support {}.".format(optype, execution_provider, supported_optype))
    elif quant_format == quantization.QuantFormat.QDQ:
        if execution_provider not in constants.STATIC_QDQ_OP_LIST_MAP:
            raise ValueError("Unsupported execution_provider {}, only support {}.".format(execution_provider, list(constants.STATIC_QDQ_OP_LIST_MAP.keys())))
        supported_optype = constants.STATIC_QDQ_OP_LIST_MAP[execution_provider]
        if optype not in supported_optype:
            raise ValueError("Unsupported optype {} for {}, only support {}.".format(optype, execution_provider, supported_optype))
    else:
        raise ValueError("Unsupported quant_format {}, only support QuantFormat.QOperator and QuantFormat.QDQ.".format(quant_format))
    return config

def static_cpu_check(config, optype, execution_provider, quant_format):
    if execution_provider != "CPUExecutionProvider":
        return config

    # only support per-tensor
    if optype in ["EmbedLayerNormalization", "Relu", "Clip", "LeakyRelu", "Sigmoid", "MaxPool", "GlobalAveragePool",
                    "Pad", "Split", "Squeeze", "Reshape", "Concat", "AveragePool", "Tile",
                    "Unsqueeze", "Transpose", "Resize", "Abs", "Shrink", "Sign", "Attention",
                    "Flatten", "Expand", "Slice", "Mod", "ReduceMax", "ReduceMin",
                    "CenterCropPad", "Add", "Mul", "ArgMax"]:
        setattr(config, "per_channel", False)

    if optype in ["Attention"]:
        setattr(config, "activation_type", onnx.TensorProto.UINT8)
    return config

def static_cuda_check(config, optype, execution_provider, quant_format):
    if execution_provider != "CUDAExecutionProvider":
        return config

    # only support per-tensor
    if optype in ["EmbedLayerNormalization", "Relu", "Clip", "LeakyRelu", "Sigmoid", "MaxPool", "GlobalAveragePool",
                    "Pad", "Split", "Squeeze", "Reshape", "Concat", "AveragePool", "Tile",
                    "Unsqueeze", "Transpose", "Resize", "Abs", "Shrink", "Sign", "Attention",
                    "Flatten", "Expand", "Slice", "Mod", "ReduceMax", "ReduceMin",
                    "CenterCropPad", "Add", "Mul", "ArgMax"]:
        setattr(config, "per_channel", False)

    if optype in ["Attention"]:
        setattr(config, "activation_type", onnx.TensorProto.INT8)
        setattr(config, "weight_type", onnx.TensorProto.INT8)
    return config

def static_dml_check(config, optype, execution_provider, quant_format):
    if execution_provider != "DmlExecutionProvider":
        return config

    # only support per-tensor
    if optype in ["Conv", "MatMul", "Mul", "Relu", "Clip", "MaxPool", "Add"]:
        setattr(config, "per_channel", False)
    return config

def static_dnnl_check(config, optype, execution_provider, quant_format):
    if execution_provider != "DnnlExecutionProvider":
        return config

    # current configurations are same as CPU EP
    return static_cpu_check(config, optype, execution_provider, quant_format)

def static_trt_check(config, optype, execution_provider, quant_format):
    if execution_provider != "TensorrtExecutionProvider":
        return config

    # only support S8S8
    if optype in ["Conv", "MatMul", "Gather", "Gemm"]:
        setattr(config, "weight_type", onnx.TensorProto.INT8)
        setattr(config, "weight_sym", True)
        setattr(config, "activation_type", onnx.TensorProto.INT8)
        setattr(config, "activation_sym", True)
        setattr(config, "per_channel", [False, True])
    else:
        setattr(config, "weight_type", onnx.TensorProto.INT8)
        setattr(config, "weight_sym", True)
        setattr(config, "activation_type", onnx.TensorProto.INT8)
        setattr(config, "activation_sym", True)
    return config

STATIC_CHECK_FUNC_LIST = [
    static_basic_check,
    static_cpu_check,
    static_cuda_check,
    static_dml_check,
    static_dnnl_check,
    static_trt_check,
]


def dynamic_basic_check(config, optype, execution_provider, quant_format=None):
    if execution_provider not in constants.DYNAMIC_OP_LIST_MAP:
        raise ValueError("Unsupported execution_provider {}, only support {}.".format(execution_provider, list(constants.DYNAMIC_OP_LIST_MAP.keys())))

    supported_optype = constants.DYNAMIC_OP_LIST_MAP[execution_provider]
    if optype not in supported_optype:
        raise ValueError("Unsupported optype {} for {}, only support {}.".format(optype, execution_provider, supported_optype))
    return config

def dynamic_cpu_check(config, optype, execution_provider, quant_format=None):
    if execution_provider != "CPUExecutionProvider":
        return config
    # TODO: add constraints for other EP
    if optype in ["FusedConv", "Conv", "EmbedLayerNormalization", "Gather", "Attention", "LSTM"]:
        setattr(config, "per_channel", False)
    return config

def dynamic_cuda_check(config, optype, execution_provider, quant_format=None):
    if execution_provider != "CUDAExecutionProvider":
        return config
    # current configurations are same as CPU EP
    return dynamic_cpu_check(config, optype, execution_provider, quant_format)

def dynamic_dml_check(config, optype, execution_provider, quant_format=None):
    if execution_provider != "DmlExecutionProvider":
        return config

    # don't support dynamic quantization
    return None

def dynamic_dnnl_check(config, optype, execution_provider, quant_format=None):
    if execution_provider != "DnnlExecutionProvider":
        return config
    # current configurations are same as CPU EP
    return dynamic_cpu_check(config, optype, execution_provider, quant_format)

def dynamic_trt_check(config, optype, execution_provider, quant_format=None):
    if execution_provider != "TensorrtExecutionProvider":
        return config

    # don't support dynamic quantization
    return None

DYNAMIC_CHECK_FUNC_LIST = [
    dynamic_basic_check,
    dynamic_cpu_check,
    dynamic_cuda_check,
    dynamic_dml_check,
    dynamic_dnnl_check,
    dynamic_trt_check,
]
