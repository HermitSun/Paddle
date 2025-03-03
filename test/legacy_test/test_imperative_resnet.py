# Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
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

import unittest

import numpy as np
from test_imperative_base import new_program_scope
from utils import DyGraphProgramDescTracerTestHelper

import paddle
from paddle import base
from paddle.base import core
from paddle.base.dygraph.base import to_variable
from paddle.base.layer_helper import LayerHelper
from paddle.nn import BatchNorm

# NOTE(zhiqiu): run with FLAGS_cudnn_deterministic=1

batch_size = 8
train_parameters = {
    "input_size": [3, 224, 224],
    "input_mean": [0.485, 0.456, 0.406],
    "input_std": [0.229, 0.224, 0.225],
    "learning_strategy": {
        "name": "piecewise_decay",
        "batch_size": batch_size,
        "epochs": [30, 60, 90],
        "steps": [0.1, 0.01, 0.001, 0.0001],
    },
    "batch_size": batch_size,
    "lr": 0.1,
    "total_images": 1281164,
}


def optimizer_setting(params, parameter_list=None):
    ls = params["learning_strategy"]
    if ls["name"] == "piecewise_decay":
        if "total_images" not in params:
            total_images = 1281167
        else:
            total_images = params["total_images"]
        batch_size = ls["batch_size"]
        step = int(total_images / batch_size + 1)

        bd = [step * e for e in ls["epochs"]]
        base_lr = params["lr"]
        lr = []
        lr = [base_lr * (0.1**i) for i in range(len(bd) + 1)]
        if base.in_dygraph_mode():
            optimizer = paddle.optimizer.SGD(
                learning_rate=0.01, parameters=parameter_list
            )
        else:
            optimizer = paddle.optimizer.SGD(learning_rate=0.01)
        # TODO(minqiyang): Add learning rate scheduler support to dygraph mode
        #  optimizer = base.optimizer.Momentum(
        #  learning_rate=params["lr"],
        #  learning_rate=paddle.optimizer.lr.piecewise_decay(
        #  boundaries=bd, values=lr),
        #  momentum=0.9,
        #  regularization=paddle.regularizer.L2Decay(1e-4))

    return optimizer


class ConvBNLayer(paddle.nn.Layer):
    def __init__(
        self,
        num_channels,
        num_filters,
        filter_size,
        stride=1,
        groups=1,
        act=None,
        use_cudnn=False,
    ):
        super().__init__()

        self._conv = paddle.nn.Conv2D(
            in_channels=num_channels,
            out_channels=num_filters,
            kernel_size=filter_size,
            stride=stride,
            padding=(filter_size - 1) // 2,
            groups=groups,
            bias_attr=False,
        )

        self._batch_norm = BatchNorm(num_filters, act=act)

    def forward(self, inputs):
        y = self._conv(inputs)
        y = self._batch_norm(y)

        return y


class BottleneckBlock(paddle.nn.Layer):
    def __init__(
        self, num_channels, num_filters, stride, shortcut=True, use_cudnn=False
    ):
        super().__init__()

        self.conv0 = ConvBNLayer(
            num_channels=num_channels,
            num_filters=num_filters,
            filter_size=1,
            act='relu',
            use_cudnn=use_cudnn,
        )
        self.conv1 = ConvBNLayer(
            num_channels=num_filters,
            num_filters=num_filters,
            filter_size=3,
            stride=stride,
            act='relu',
            use_cudnn=use_cudnn,
        )
        self.conv2 = ConvBNLayer(
            num_channels=num_filters,
            num_filters=num_filters * 4,
            filter_size=1,
            act=None,
            use_cudnn=use_cudnn,
        )

        if not shortcut:
            self.short = ConvBNLayer(
                num_channels=num_channels,
                num_filters=num_filters * 4,
                filter_size=1,
                stride=stride,
                use_cudnn=use_cudnn,
            )

        self.shortcut = shortcut

    def forward(self, inputs):
        y = self.conv0(inputs)
        conv1 = self.conv1(y)
        conv2 = self.conv2(conv1)

        if self.shortcut:
            short = inputs
        else:
            short = self.short(inputs)

        y = paddle.add(x=short, y=conv2)

        layer_helper = LayerHelper(self.full_name(), act='relu')
        return layer_helper.append_activation(y)


class ResNet(paddle.nn.Layer):
    def __init__(self, layers=50, class_dim=102, use_cudnn=True):
        super().__init__()

        self.layers = layers
        supported_layers = [50, 101, 152]
        assert (
            layers in supported_layers
        ), f"supported layers are {supported_layers} but input layer is {layers}"

        if layers == 50:
            depth = [3, 4, 6, 3]
        elif layers == 101:
            depth = [3, 4, 23, 3]
        elif layers == 152:
            depth = [3, 8, 36, 3]
        num_channels = [64, 256, 512, 1024]
        num_filters = [64, 128, 256, 512]

        self.conv = ConvBNLayer(
            num_channels=3,
            num_filters=64,
            filter_size=7,
            stride=2,
            act='relu',
            use_cudnn=use_cudnn,
        )
        self.pool2d_max = paddle.nn.MaxPool2D(
            kernel_size=3, stride=2, padding=1
        )

        self.bottleneck_block_list = []
        for block in range(len(depth)):
            shortcut = False
            for i in range(depth[block]):
                bottleneck_block = self.add_sublayer(
                    'bb_%d_%d' % (block, i),
                    BottleneckBlock(
                        num_channels=num_channels[block]
                        if i == 0
                        else num_filters[block] * 4,
                        num_filters=num_filters[block],
                        stride=2 if i == 0 and block != 0 else 1,
                        shortcut=shortcut,
                        use_cudnn=use_cudnn,
                    ),
                )
                self.bottleneck_block_list.append(bottleneck_block)
                shortcut = True
        self.pool2d_avg = paddle.nn.AdaptiveAvgPool2D(1)

        self.pool2d_avg_output = num_filters[-1] * 4 * 1 * 1

        import math

        stdv = 1.0 / math.sqrt(2048 * 1.0)

        self.out = paddle.nn.Linear(
            self.pool2d_avg_output,
            class_dim,
            weight_attr=base.param_attr.ParamAttr(
                initializer=paddle.nn.initializer.Uniform(-stdv, stdv)
            ),
        )

    def forward(self, inputs):
        y = self.conv(inputs)
        y = self.pool2d_max(y)
        for bottleneck_block in self.bottleneck_block_list:
            y = bottleneck_block(y)
        y = self.pool2d_avg(y)
        y = paddle.reshape(y, shape=[-1, self.pool2d_avg_output])
        y = self.out(y)
        y = paddle.nn.functional.softmax(y)
        return y


class TestDygraphResnet(unittest.TestCase):
    def reader_decorator(self, reader):
        def _reader_imple():
            for item in reader():
                doc = np.array(item[0]).reshape(3, 224, 224)
                label = np.array(item[1]).astype('int64').reshape(1)
                yield doc, label

        return _reader_imple

    def test_resnet_float32(self):
        seed = 90

        batch_size = train_parameters["batch_size"]
        batch_num = 10

        traced_layer = None

        with base.dygraph.guard():
            paddle.seed(seed)
            paddle.framework.random._manual_program_seed(seed)

            resnet = ResNet()
            optimizer = optimizer_setting(
                train_parameters, parameter_list=resnet.parameters()
            )
            np.random.seed(seed)

            train_reader = paddle.batch(
                paddle.dataset.flowers.train(use_xmap=False),
                batch_size=batch_size,
            )

            dy_param_init_value = {}
            for param in resnet.parameters():
                dy_param_init_value[param.name] = param.numpy()

            helper = DyGraphProgramDescTracerTestHelper(self)
            program = None

            for batch_id, data in enumerate(train_reader()):
                if batch_id >= batch_num:
                    break

                dy_x_data = np.array(
                    [x[0].reshape(3, 224, 224) for x in data]
                ).astype('float32')
                y_data = (
                    np.array([x[1] for x in data])
                    .astype('int64')
                    .reshape(batch_size, 1)
                )

                img = to_variable(dy_x_data)
                label = to_variable(y_data)
                label.stop_gradient = True

                out = None
                out = resnet(img)

                if traced_layer is not None:
                    resnet.eval()
                    traced_layer._switch(is_test=True)
                    out_dygraph = resnet(img)
                    out_static = traced_layer([img])
                    traced_layer._switch(is_test=False)
                    helper.assertEachVar(out_dygraph, out_static)
                    resnet.train()

                loss = paddle.nn.functional.cross_entropy(
                    input=out, label=label, reduction='none', use_softmax=False
                )
                avg_loss = paddle.mean(x=loss)

                dy_out = avg_loss.numpy()

                if batch_id == 0:
                    for param in resnet.parameters():
                        if param.name not in dy_param_init_value:
                            dy_param_init_value[param.name] = param.numpy()

                avg_loss.backward()

                dy_grad_value = {}
                for param in resnet.parameters():
                    if param.trainable:
                        np_array = np.array(
                            param._grad_ivar().value().get_tensor()
                        )
                        dy_grad_value[
                            param.name + core.grad_var_suffix()
                        ] = np_array

                optimizer.minimize(avg_loss)
                resnet.clear_gradients()

                dy_param_value = {}
                for param in resnet.parameters():
                    dy_param_value[param.name] = param.numpy()

        with new_program_scope():
            paddle.seed(seed)
            paddle.framework.random._manual_program_seed(seed)

            exe = base.Executor(
                base.CPUPlace()
                if not core.is_compiled_with_cuda()
                else base.CUDAPlace(0)
            )

            resnet = ResNet()
            optimizer = optimizer_setting(train_parameters)

            np.random.seed(seed)
            train_reader = paddle.batch(
                paddle.dataset.flowers.train(use_xmap=False),
                batch_size=batch_size,
            )

            img = paddle.static.data(
                name='pixel', shape=[-1, 3, 224, 224], dtype='float32'
            )
            label = paddle.static.data(
                name='label', shape=[-1, 1], dtype='int64'
            )
            out = resnet(img)
            loss = paddle.nn.functional.cross_entropy(
                input=out, label=label, reduction='none', use_softmax=False
            )
            avg_loss = paddle.mean(x=loss)
            optimizer.minimize(avg_loss)

            # initialize params and fetch them
            static_param_init_value = {}
            static_param_name_list = []
            static_grad_name_list = []
            for param in resnet.parameters():
                static_param_name_list.append(param.name)
            for param in resnet.parameters():
                if param.trainable:
                    static_grad_name_list.append(
                        param.name + core.grad_var_suffix()
                    )

            out = exe.run(
                base.default_startup_program(),
                fetch_list=static_param_name_list,
            )

            for i in range(len(static_param_name_list)):
                static_param_init_value[static_param_name_list[i]] = out[i]

            for batch_id, data in enumerate(train_reader()):
                if batch_id >= batch_num:
                    break

                static_x_data = np.array(
                    [x[0].reshape(3, 224, 224) for x in data]
                ).astype('float32')
                y_data = (
                    np.array([x[1] for x in data])
                    .astype('int64')
                    .reshape([batch_size, 1])
                )

                if traced_layer is not None:
                    traced_layer([static_x_data])

                fetch_list = [avg_loss.name]
                fetch_list.extend(static_param_name_list)
                fetch_list.extend(static_grad_name_list)
                out = exe.run(
                    base.default_main_program(),
                    feed={"pixel": static_x_data, "label": y_data},
                    fetch_list=fetch_list,
                )

                static_param_value = {}
                static_grad_value = {}
                static_out = out[0]
                param_start_pos = 1
                grad_start_pos = len(static_param_name_list) + param_start_pos
                for i in range(
                    param_start_pos,
                    len(static_param_name_list) + param_start_pos,
                ):
                    static_param_value[
                        static_param_name_list[i - param_start_pos]
                    ] = out[i]
                for i in range(
                    grad_start_pos, len(static_grad_name_list) + grad_start_pos
                ):
                    static_grad_value[
                        static_grad_name_list[i - grad_start_pos]
                    ] = out[i]

        print("static", static_out)
        print("dygraph", dy_out)
        np.testing.assert_allclose(static_out, dy_out, rtol=1e-05)

        self.assertEqual(len(dy_param_init_value), len(static_param_init_value))

        for key, value in static_param_init_value.items():
            np.testing.assert_allclose(
                value, dy_param_init_value[key], rtol=1e-05
            )
            self.assertTrue(np.isfinite(value.all()))
            self.assertFalse(np.isnan(value.any()))

        self.assertEqual(len(dy_grad_value), len(static_grad_value))
        for key, value in static_grad_value.items():
            np.testing.assert_allclose(value, dy_grad_value[key], rtol=1e-05)
            self.assertTrue(np.isfinite(value.all()))
            self.assertFalse(np.isnan(value.any()))

        self.assertEqual(len(dy_param_value), len(static_param_value))
        for key, value in static_param_value.items():
            np.testing.assert_allclose(value, dy_param_value[key], rtol=1e-05)
            self.assertTrue(np.isfinite(value.all()))
            self.assertFalse(np.isnan(value.any()))


if __name__ == '__main__':
    paddle.enable_static()
    unittest.main()
