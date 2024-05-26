#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Intel Corporation
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
"""Operators for onnx model."""

from os import path
import glob
from onnx_neural_compressor.algorithms.post_training_quant.operators import base_op

modules = glob.glob(path.join(path.dirname(__file__), "*.py"))

for f in modules:
    if path.isfile(f) and not f.startswith("__") and not f.endswith("__init__.py"):
        __import__(path.basename(f)[:-3], globals(), locals(), level=1)

OPERATORS = base_op.OPERATORS