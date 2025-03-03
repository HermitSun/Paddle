# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random
import unittest

import numpy as np

import paddle
import paddle.distributed as dist
from paddle import nn
from paddle.distributed import Replicate, Shard
from paddle.io import DataLoader

BATCH_SIZE = 4
BATCH_NUM = 4
SEQ_LEN = 2
IMAGE_SIZE = 16
CLASS_NUM = 8


def create_numpy_like_random(name):
    return paddle.ParamAttr(
        name=name, initializer=paddle.nn.initializer.Uniform(0, 1)
    )


class RandomDataset(paddle.io.Dataset):
    def __init__(self, images, labels, num_samples):
        self.images = images
        self.labels = labels
        self.num_samples = num_samples

    def __getitem__(self, idx):
        return self.images[idx], self.labels[idx]

    def __len__(self):
        return self.num_samples


class MLP(nn.Layer):
    def __init__(self, mesh, shard_weight=False, param_prefix=""):
        super().__init__()
        self._mesh = mesh
        self.shard_weight = shard_weight
        weight_attr_0 = create_numpy_like_random(param_prefix + "_0")
        weight_attr_1 = create_numpy_like_random(param_prefix + "_1")

        self.linear_0 = nn.Linear(IMAGE_SIZE, IMAGE_SIZE, weight_attr_0)
        self.linear_1 = nn.Linear(IMAGE_SIZE, CLASS_NUM, weight_attr_1)
        if shard_weight:
            self.linear_0.weight = dist.shard_tensor(
                self.linear_0.weight,
                self._mesh,
                [Shard(1)],
                stop_gradient=False,
            )
            self.linear_1.weight = dist.shard_tensor(
                self.linear_1.weight,
                self._mesh,
                [Shard(0)],
                stop_gradient=False,
            )
        self.relu = nn.ReLU()

    def _inner_forward_fn(self, x):
        out = self.linear_0(x)
        out = self.relu(out)
        out = self.linear_1(out)
        return out

    def forward(self, x):
        return self._inner_forward_fn(x)


class DemoNetPP(nn.Layer):
    def __init__(
        self,
        mesh0,
        mesh1,
    ):
        super().__init__()
        self._mesh0 = mesh0
        self._mesh1 = mesh1
        self.mlp0 = MLP(mesh0, False, "block0")
        self.mlp1 = MLP(mesh1, False, "block1")
        self.varnmes = []

    def forward(self, x):
        # stage0
        dist.shard_tensor(
            x,
            self._mesh0,
            [Replicate()],
        )
        out0 = self.mlp0(x)

        self.varnmes.append(out0.name)
        out0 = dist.reshard(out0, self._mesh1, [Replicate()])
        self.varnmes.append(out0.name)

        # stage1
        out1 = self.mlp1(out0)

        return out1


class TestStaticReshard(unittest.TestCase):
    def __init__(self):
        self._seed = 1234
        self.set_random_seed(self._seed)

    def set_random_seed(self, seed):
        random.seed(seed)
        np.random.seed(seed)
        paddle.seed(seed)

    def create_data_loader(self):
        images = np.random.rand(BATCH_SIZE, IMAGE_SIZE).astype('float32')
        labels = np.random.rand(BATCH_SIZE, CLASS_NUM).astype('float32')
        dataset = RandomDataset(images, labels, BATCH_SIZE)
        loader = DataLoader(dataset, batch_size=BATCH_SIZE)
        return loader

    def test_reshard_mesh(self):
        mesh0 = dist.ProcessMesh([0], dim_names=["x"])
        mesh1 = dist.ProcessMesh([1], dim_names=["x"])

        dy2static_layer = DemoNetPP(mesh0, mesh1)

        dy2static_opt = paddle.optimizer.SGD(
            learning_rate=0.1, parameters=dy2static_layer.parameters()
        )

        loss_fn = nn.MSELoss()

        # static training
        data_loader = self.create_data_loader()
        dist_model, dist_loader = dist.to_static(
            dy2static_layer, data_loader, loss_fn, dy2static_opt
        )

        program = dist_model._engine._dist_contexts["train"].dist_main_programs[
            dist_model._engine._cur_rank
        ]
        ops = program.global_block().ops

        if dist_model._engine._cur_rank == 0:
            op_names = [op.type for op in ops[:7]]
            assert op_names == [
                'matmul_v2',
                'elementwise_add',
                'relu',
                'matmul_v2',
                'elementwise_add',
                'send_v2',
                'recv_v2',
            ]
        elif dist_model._engine._cur_rank == 1:
            op_names = [op.type for op in ops[:9]]
            assert op_names == [
                'recv_v2',
                'assign',
                'assign',
                'matmul_v2',
                'elementwise_add',
                'relu',
                'matmul_v2',
                'elementwise_add',
                'elementwise_sub',
            ]

    def run_test_case(self):
        self.test_reshard_mesh()


if __name__ == '__main__':
    TestStaticReshard().run_test_case()
