#   Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
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
import copy
from collections import defaultdict
from typing import Callable

import numpy as np

import paddle
import paddle.distributed as dist
from paddle import nn
from paddle.base import unique_name
from paddle.base.framework import (
    EagerParamBase,
    Variable,
    default_main_program,
)
from paddle.distributed.auto_parallel import Engine, strategy as auto_strategy
from paddle.distributed.auto_parallel.interface import (
    shard_tensor as shard_tensor_static,
)
from paddle.distributed.auto_parallel.placement_type import to_placements
from paddle.distributed.auto_parallel.static.completion import (
    mark_as_sharding_propagation_skip_op,
)
from paddle.distributed.auto_parallel.static.dist_context import (
    get_default_distributed_context,
)
from paddle.distributed.auto_parallel.static.dist_op import DistributedOperator
from paddle.distributed.auto_parallel.static.utils import (
    convert_to_dims_mapping,
    get_dist_attr,
)
from paddle.framework import core

from .placement_type import check_placements_equal, get_shard_spec

# There are the auto parallel API of the unified version of dynamic and static mode.
# Some APIs have the same name with the previous APIs implementation, which are
# a temporary state, and the APIs here will eventually be used.

# Part1: Shard attributes related APIs


class DistAttr(core.TensorDistAttr):
    """
    DistAttr specifies how tensors are distributed or sliced on ProcessMesh.

    Args:
        mesh(paddle.distributed.ProcessMesh): The `ProcessMesh` object describes the Cartesian topology of the used processes.
        sharding_specs(list[str|None]): The specification describing how to shard the Tensor.

    Examples:
        .. code-block:: python

            >>> import paddle
            >>> import paddle.distributed as dist

            >>> mesh = dist.ProcessMesh([[2, 4, 5], [0, 1, 3]], dim_names=['x', 'y'])
            >>> dist_attr = dist.DistAttr(mesh=mesh, sharding_specs=['x', 'y'])

            >>> print(dist_attr)

    """

    def __init__(self, mesh, sharding_specs):
        # 1. inputs checking
        if not isinstance(mesh, core.ProcessMesh):
            raise ValueError(
                "The mesh must be an instance of paddle.distributed.ProcessMesh."
            )
        if not isinstance(sharding_specs, list):
            raise ValueError("The sharding_specs must be an instance of list.")
        assert all(
            isinstance(dim_name, str) or dim_name is None
            for dim_name in sharding_specs
        ), 'The dimension name in sharding_specs must be an instance of str.'

        self._sharding_specs = sharding_specs
        dims_mapping = [
            mesh.dim_names.index(dim_name) if dim_name is not None else -1
            for dim_name in sharding_specs
        ]

        # 2. init core.TensorDistAttr
        core.TensorDistAttr.__init__(self)

        self.process_mesh = mesh
        self.dims_mapping = dims_mapping
        self.mark_annotated("process_mesh")
        self.mark_annotated("dims_mapping")

    @property
    def sharding_specs(self):
        """
        Get sharding_specs of the dist_attr
        Returns:
            list[str]: sharding_specs
        """
        return self._sharding_specs


# Part2: DistTensor construction related APIs


def shard_tensor(
    data, mesh, placements, dtype=None, place=None, stop_gradient=None
):
    """
    Creates a distributed Tensor (i.e., Tensor with distributed attributes or DistTensor for short)
    from the input data, which can be a scalar, tuple, list, numpy.ndarray, or paddle.Tensor.

    If the ``data`` is already a Tensor, it will be transformed into a distributed Tensor.

    Args:
        data(scalar|tuple|list|ndarray|Tensor): Initial data for the tensor.
            Can be a scalar, list, tuple, numpy.ndarray, paddle.Tensor.
        mesh(paddle.distributed.ProcessMesh): The `ProcessMesh` object describes the Cartesian topology of the used processes.
        placements(list[paddle.distributed.Placement]): the placements describe how to place the tensor on ProcessMesh, it can
            be Shard, Replicate and Partial.
        dtype(str|np.dtype, optional): The desired data type of returned tensor.
            It Can be 'bool' , 'float16' , 'float32' , 'float64' , 'int8' , 'int16' , 'int32' , 'int64' , 'uint8',
            'complex64' , 'complex128'. Default: None. If None, the the dtype is infered from ``data``
            except for python float number, in which case the dtype is infered from ``get_default_type`` .
        place(CPUPlace|CUDAPinnedPlace|CUDAPlace|str, optional): The place to allocate Tensor. Can be
            CPUPlace, CUDAPinnedPlace, CUDAPlace. Default: None, means global place. If ``place`` is
            string, It can be ``cpu``, ``gpu:x`` and ``gpu_pinned``, where ``x`` is the index of the GPUs.
        stop_gradient(bool, optional): Whether to block the gradient propagation of Autograd. If
            ``stop_gradient`` is None, set the returned Tensor's ``stop_gradient`` identical as the
            ``data.stop_gradient`` when ``data`` has ``stop_gradient`` attribute and True otherwise.
            Default: None.

    Returns:
        Tensor: A Tensor constructed from ``data`` with distributed attributes.

    Examples:
        .. code-block:: python

            >>> import paddle
            >>> import paddle.distributed as dist

            >>> mesh = dist.ProcessMesh([[2, 4, 5], [0, 1, 3]], dim_names=['x', 'y'])

            >>> # dense tensor
            >>> a = paddle.to_tensor([[1,2,3],
            ...                       [5,6,7]])

            >>> # doctest: +REQUIRES(env:DISTRIBUTED)
            >>> # distributed tensor
            >>> d_tensor = dist.shard_tensor(a, mesh, [dist.Shard(0), dist.Shard(1)])

            >>> print(d_tensor)

    """
    if place is None:
        place = paddle.framework._current_expected_place()
    place = paddle.framework._get_paddle_place(place)

    # 1. create dense tensor
    # `paddle.to_tensor` supports both dynamic and static mode
    if stop_gradient is None:
        stop_gradient = getattr(data, "stop_gradient", True)
    tensor = paddle.to_tensor(
        data, dtype=dtype, place=place, stop_gradient=stop_gradient
    )

    if paddle.in_dynamic_mode():
        # here the dist tensor is deep copy constructed
        if isinstance(data, EagerParamBase):
            return EagerParamBase.from_tensor(
                tensor,
                process_mesh=mesh,
                placements=placements,
                **tensor.__dict__,
            )
        else:
            return paddle.Tensor(
                tensor, process_mesh=mesh, placements=placements, place=place
            )
    else:
        # TODO(zhiqiu): we need to refine the static shard_tensor
        sharding_specs = get_shard_spec(mesh, placements, tensor.ndim)
        return shard_tensor_static(tensor, mesh, sharding_specs)


def dtensor_from_local(local_tensor, mesh, placements):
    # assume the each rank has the same tensor shape for now, just use the local shape to calculate the global shape
    global_dims = list(local_tensor.shape)
    for idx, placement in enumerate(placements):
        if placement.is_shard():
            shard_dim = placement.get_dim()
            local_dim_size = global_dims[shard_dim]
            global_dims[shard_dim] = local_dim_size * mesh.shape[idx]

    place = paddle.framework._current_expected_place()
    place = paddle.framework._get_paddle_place(place)

    return paddle.Tensor(
        local_tensor,
        dims=global_dims,
        process_mesh=mesh,
        placements=placements,
        place=place,
    )


def dtensor_from_fn(fn, mesh, placements, *args, **kwargs):
    """
    Construct a Distributed Tensor from a function of arguments.

    Args:
        fn (callable): A callable function that takes arguments of Distributed Tensor and returns tensor.
        mesh(paddle.distributed.ProcessMesh): The `ProcessMesh` object describes the Cartesian topology of the used processes.
        placements(list[paddle.distributed.Placement]): the placements describe how to place the tensor on ProcessMesh, it can
            be Shard, Replicate and Partial.
        *args (tuple): A tuple of arguments to be passed to the ``fn`` function.
        **kwargs (dict): A dict of arguments to be passed to the ``fn`` function.

    Retruns:
        Tensor: A Tensor constructed from ``fn`` with distributed attributes.

    Examples:
        .. code-block:: python

            >>> import paddle
            >>> import paddle.distributed as dist
            >>> # Create a distributed attribute
            >>> mesh = dist.ProcessMesh([0, 1], dim_names=["x"])
            >>> # Call the function dtensor_from_fn with dist_attr parameter
            >>> d_tensor = dist.dtensor_from_fn(paddle.ones, mesh, [dist.Replicate()], shape=[1])
            >>> print(d_tensor)

    """
    tensor = fn(*args, **kwargs)
    return shard_tensor(tensor, mesh, placements)


# Part3: Data conversion related APIs


def reshard(dist_tensor, mesh, placements):
    """
    Reshard a distributed ``paddle.Tensor`` with given distributed attributes.

    Args:
        dist_tensor(Tensor): the distributed tensor to be resharded.
        mesh(paddle.distributed.ProcessMesh): The `ProcessMesh` object describes the Cartesian topology of the used processes.
        placements(list[paddle.distributed.Placement]): the placements describe how to place the tensor on ProcessMesh, it can
            be Shard, Replicate and Partial.

    Returns:
        Tensor: A Distributed Tensor reshared with distributed attributes.

    Examples:
        .. code-block:: python

            >>> import paddle
            >>> import paddle.distributed as dist

            >>> mesh = dist.ProcessMesh([0, 1], dim_names=["x"])

            >>> # dense tensor
            >>> a = paddle.ones([10, 20])

            >>> # doctest: +REQUIRES(env:DISTRIBUTED)
            >>> # distributed tensor
            >>> d_tensor = dist.shard_tensor(a, mesh, [dist.Partial()])

            >>> out_d_tensor = dist.reshard(d_tensor, mesh, [dist.Replicate()])

            >>> print(out_d_tensor)

    """

    if paddle.framework.in_dynamic_mode():
        # TODO(LiYuRio): static logic here, reshard should be changed for dygraph logic
        # when reshard has been changed align dygraph logic, delete it.
        sharding_specs = get_shard_spec(mesh, placements, dist_tensor.ndim)
        dist_attr = DistAttr(mesh, sharding_specs)
        partial_dims = []
        for i, p in enumerate(placements):
            if isinstance(p, dist.Partial):
                partial_dims.append(i)
        if len(partial_dims) > 0:
            dist_attr._set_partial_dims(partial_dims)

        return paddle.base.core.reshard(dist_tensor, dist_attr)
    else:
        assert isinstance(
            dist_tensor, Variable
        ), "in dy2static mode, reshard's input should be Variable, but got [{}]".format(
            dist_tensor
        )
        sharding_specs = get_shard_spec(mesh, placements, dist_tensor.ndim)
        main_program = default_main_program()
        default_dist_ctx = get_default_distributed_context()

        # output variable
        out_var = main_program.current_block().create_var(
            name=unique_name.generate_with_ignorable_key(
                ".".join(['reshard_api', 'tmp'])
            ),
            dtype=dist_tensor.dtype,
            shape=dist_tensor.shape,
            type=dist_tensor.type,
            persistable=dist_tensor.persistable,
            stop_gradient=dist_tensor.stop_gradient,
        )

        # transition op
        # optimization in future to remove redundant D2D memory copy
        target_dims_mapping = convert_to_dims_mapping(sharding_specs, mesh)
        trans_op = main_program.current_block().append_op(
            type='assign',
            inputs={'X': [dist_tensor]},
            outputs={'Out': [out_var]},
        )
        dist_op = DistributedOperator(trans_op)
        dist_op.dist_attr.process_mesh = mesh
        dist_op.dist_attr.mark_annotated("process_mesh")
        dist_op.dist_attr.chunk_id = 0

        input_dist_attr = dist_op.dist_attr.get_input_dist_attr(
            dist_tensor.name
        )
        input_dist_attr.dims_mapping = target_dims_mapping
        input_dist_attr.mark_annotated("dims_mapping")
        output_dist_attr = dist_op.dist_attr.get_output_dist_attr(out_var.name)
        output_dist_attr.dims_mapping = target_dims_mapping
        output_dist_attr.mark_annotated("dims_mapping")

        default_dist_ctx.add_dist_op_for_program(dist_op)
        mark_as_sharding_propagation_skip_op(trans_op)
        # trans_op = shard_op_static(paddle.assign, mesh, [sharding_specs])
        # out_var = trans_op(dist_tensor)

        return out_var


def shard_layer(
    layer: nn.Layer,
    process_mesh: dist.ProcessMesh,
    shard_fn: Callable = None,
    input_fn: Callable = None,
    output_fn: Callable = None,
) -> nn.Layer:
    """
    Converts all layer's parameters to DistTensor parameters according to
    the `shard_fn` specified. It could also control the conversion of input
    or output of the layer by specifying the `input_fn` and `output_fn`.
    (i.e. convert the input to `paddle.Tensor` with distributed attributes,
    convert output back to `paddle.Tensor` without distributed attributes.)

    The `shard_fn` should have the following signature:

        def shard_fn(layer_name, layer, process_mesh) -> None

    The `input_fn` should have the following signature:

        def input_fn(inputs, process_mesh) -> list(paddle.Tensor)

    In general, the type of `input_fn` return value is paddle.Tensor with distributed attributes.

    The `output_fn` should have the following signature:

        def output_fn(outputs, process_mesh) -> list(paddle.Tensor)

    In general, the type of `output_fn` return value is paddle.Tensor with distributed attributes.

    Args:
        layer (paddle.nn.Layer): The Layer object to be shard.
        process_mesh (paddle.distributed.ProcessMesh): The `ProcessMesh` information
            to be place the input `layer`.
        shard_fn (Callable): The function to shard layer parameters across
            the `process_mesh`. If not specified, by default we replicate
            all parameters of the layer across the `process_mesh`.
        input_fn (Callable): Specify how the input of the layer is sharded.
            The `input_fn` will be registered for the Layer as a `forward pre-hook`.
            By default we do not shard the input.
        output_fn (Callable): Specify how the output of the layer is sharded or
            convert it back to `paddle.Tensor` without distributed attributes.
            The `output_fn` will be registered for the Layer as `forward post-hook`.
            By default we do not shard or convert the output.
    Returns:
        Layer: A layer that contains parameters/buffers
            that are all `paddle.Tensor` with distributed attributes.

    Examples:
        .. code-block:: python

            >>> import paddle
            >>> import paddle.distributed as dist

            >>> mesh = dist.ProcessMesh([0, 1], dim_names=["x"])

            >>> class MLP(paddle.nn.Layer):
            ...     def __init__(self):
            ...         super().__init__()
            ...         self.fc1 = paddle.nn.Linear(8, 8)
            ...         self.fc2 = paddle.nn.Linear(8, 8)
            ...
            ...     def forward(self, input):
            ...         return self.fc2(self.fc1(input))

            >>> def shard_fn(layer_name, layer, process_mesh):
            ...     if layer_name == 'fc1':
            ...         layer.weight = dist.shard_tensor(layer.weight, process_mesh, [dist.Shard(0)])

            >>> layer = MLP()
            >>> layer = dist.shard_layer(layer, mesh, shard_fn)
            >>> print(layer)

            >>> # This case need to be excuted in multi-card environment
            >>> # export CUDA_VISIBLE_DEVICES=0,1
            >>> # python -m paddle.distributed.launch {test_case}.py
    """
    # Ensure that process_mesh is not an empty object
    if process_mesh is None:
        raise ValueError("The argument `process_mesh` cannot be empty.")

    # Check the legality of process_mesh
    if not isinstance(process_mesh, dist.ProcessMesh):
        raise ValueError(
            "The argument `process_mesh` is not `dist.ProcessMesh` type."
        )

    def replicate_layer_params_and_buffers(
        layer: nn.Layer, mesh: dist.ProcessMesh
    ) -> None:
        for key, param in layer._parameters.items():
            if param is not None and not param.is_dist():
                placements = [
                    paddle.distributed.Replicate()
                    for _ in range(len(param.shape))
                ]
                layer.add_parameter(
                    key,
                    shard_tensor(param, mesh, placements),
                )
            else:
                # do nothing, the dist parameters has already been shard by shard_fn
                pass
        for key, buffer in layer._buffers.items():
            if buffer is not None and not buffer.is_dist():
                placements = [
                    paddle.distributed.Replicate()
                    for _ in range(len(buffer.shape))
                ]
                layer.register_buffer(
                    key,
                    shard_tensor(buffer, mesh, placements),
                )
            else:
                # do nothing, the dist buffers has already been shard by shard_fn
                pass

    if paddle.in_dynamic_mode():
        if shard_fn is None:
            # if shard_fn not specified, by default replicate
            # all layer's parameters and buffers
            for name, sublayers in layer.named_sublayers(include_self=True):
                replicate_layer_params_and_buffers(sublayers, process_mesh)
        else:
            # apply shard_fn to sublayers, contains self
            for name, sublayers in layer.named_sublayers(include_self=True):
                shard_fn(name, sublayers, process_mesh)
                # shard_fn may not deal with all parameters and buffers,
                # the parameters and buffers that are not shard by shard_fn
                # still need to be shard to replicated
                replicate_layer_params_and_buffers(sublayers, process_mesh)

        # register input_fn as layer's forward pre hook
        if input_fn is not None:
            layer.register_forward_pre_hook(
                lambda _, inputs: input_fn(inputs, process_mesh)
            )
        # register output_fn as layer's forward post hook
        if output_fn is not None:
            layer.register_forward_post_hook(
                lambda _, inputs, outputs: output_fn(outputs, process_mesh)
            )

        return layer
    else:
        # TODO(chenweihang): Support static mode branch later.
        raise NotImplementedError(
            "`paddle.distributed.shard_layer` only supports dynamic graph mode "
            "now. It will be supported for static graph mode later."
        )


class _ShardOptimizer:
    def __init__(self, optimizer, shard_fn=None):
        assert (
            optimizer is not None
        ), "The argument `optimizer` cannot be empty."
        assert isinstance(
            optimizer, (paddle.optimizer.AdamW, paddle.optimizer.SGD)
        ), "`paddle.distributed.ShardOptimizer` only supports AdamW and SGD optimizer for now."

        self.target_block = (
            paddle.base.framework.default_main_program().global_block()
        )
        optimizer.helper = paddle.base.layer_helper.LayerHelper(
            optimizer.__class__.__name__
        )
        # solve global_clip for auto_parallel
        self._shard_clip = False
        self._generate_flag = False
        if (
            hasattr(optimizer, "_grad_clip")
            and optimizer._grad_clip is not None
            and isinstance(optimizer._grad_clip, paddle.nn.ClipGradByGlobalNorm)
        ):
            self._shard_clip = True
        self._inner_opt = optimizer
        self._shard_fn = shard_fn

    def _shard_accumulator(self, param):
        # create the accumulators
        self._inner_opt._create_accumulators(self.target_block, [param])

        target_name = param.name
        if param.name in self._inner_opt._master_weights.keys():
            target_name = self._inner_opt._master_weights[param.name].name

        # shard the accumulators
        for key in self._inner_opt._accumulators.keys():
            accumulator = self._inner_opt._accumulators[key][target_name]
            if accumulator.is_dist():
                continue
            if self._shard_fn is not None:
                self._inner_opt._accumulators[key][
                    target_name
                ] = self._shard_fn(key, param, accumulator)
            else:
                if param.is_dist():
                    if 'beta' not in key:
                        # If param is a dist tensor should keep the shard info
                        # for accumulators except beta.
                        placements = param.placements
                    else:
                        # The beta should be replicated cross param's mesh
                        placements = [
                            dist.Replicate()
                            for _ in range(len(param.process_mesh.shape))
                        ]
                    self._inner_opt._accumulators[key][
                        target_name
                    ] = shard_tensor(
                        accumulator,
                        mesh=param.process_mesh,
                        placements=placements,
                    )

    def generate_pp_mesh(self, all_process_ids=[]):
        pp_mesh = None
        if len(all_process_ids) <= 1:
            return pp_mesh
        else:
            mesh = np.array(all_process_ids)
            for i in range(mesh.shape[-1]):
                ranks = mesh[:, i].tolist()
                if dist.get_rank() in ranks:
                    pp_mesh = dist.ProcessMesh(ranks)
        return pp_mesh

    def step(self):
        if not isinstance(self._inner_opt._parameter_list[0], dict):
            params_grads = []
            all_process_ids = []
            for param in self._inner_opt._parameter_list:
                if param.stop_gradient:
                    continue
                if param._grad_ivar() is not None:
                    grad_var = param._grad_ivar()
                    params_grads.append((param, grad_var))
                if (
                    not self._generate_flag
                    and self._shard_clip
                    and param.is_dist()
                ):
                    if param.process_mesh.process_ids not in all_process_ids:
                        all_process_ids.append(param.process_mesh.process_ids)
            if not self._generate_flag and self._shard_clip:
                self._inner_opt._grad_clip._pp_mesh = self.generate_pp_mesh(
                    all_process_ids
                )
                self._generate_flag = True
            for p, g in params_grads:
                self._shard_accumulator(p)
            self._inner_opt._apply_optimize(
                loss=None, startup_program=None, params_grads=params_grads
            )
        else:
            for param_group in self._inner_opt._param_groups:
                params_grads = defaultdict(lambda: [])
                all_process_ids = []
                shard_clip_flag = False
                for param in param_group['params']:
                    if param.stop_gradient:
                        continue
                    if param._grad_ivar() is not None:
                        grad_var = param._grad_ivar()
                        params_grads['params'].append((param, grad_var))
                    if (
                        not self._generate_flag
                        and "grad_clip" in param_group.keys()
                        and isinstance(
                            param_group["grad_clip"],
                            paddle.nn.ClipGradByGlobalNorm,
                        )
                        and param.is_dist()
                    ):
                        if (
                            param.process_mesh.process_ids
                            not in all_process_ids
                        ):
                            all_process_ids.append(
                                param.process_mesh.process_ids
                            )
                            shard_clip_flag = True

                if shard_clip_flag:
                    param_group["grad_clip"]._pp_mesh = self.generate_pp_mesh(
                        all_process_ids
                    )
                params_grads.update(
                    {k: v for k, v in param_group.items() if k != 'params'}
                )
                for p, g in params_grads['params']:
                    self._shard_accumulator(p)
                self._inner_opt._apply_optimize(
                    loss=None, startup_program=None, params_grads=params_grads
                )
            # only generate once.
            self._generate_flag = True

    def state_dict(self):
        """
        Create and shard the optimizer states e.g., acumulators and master_weights before load_state_dict.
        If training has already started or the optimizer states are already created and sharded, do nothing.
        """
        state_dict = self._inner_opt.state_dict()
        # training has already started.
        param_list = []
        if isinstance(self._inner_opt._parameter_list[0], dict):
            for param_group in self._inner_opt._parameter_list:
                param_list += param_group["params"]
        else:
            param_list = self._inner_opt._parameter_list
        for param in param_list:
            if param.stop_gradient:
                continue
            if hasattr(param, "main_grad"):
                if param.main_grad is not None:
                    return state_dict
            else:
                if param.grad is not None:
                    return state_dict

        # TODO(pangengzheng): deal with master_weights and LR_Scheduler later
        # the optimizer states are already created and sharded
        if any(
            v.is_dist()
            for k, v in state_dict.items()
            if k not in ["master_weights", "LR_Scheduler"]
        ):
            return state_dict

        # create and shard the optimizer states
        # fake the parameter gradient and invoke step to implicitly create the optimizer states.
        if not isinstance(self._inner_opt._parameter_list[0], dict):
            for param in self._inner_opt._parameter_list:
                if param.stop_gradient:
                    continue
                if hasattr(param, "main_grad"):
                    if param.main_grad is not None:
                        raise ValueError(
                            f"gradient should be None, but is {param.main_grad}"
                        )
                    param.main_grad = paddle.zeros_like(
                        param, dtype=paddle.float32
                    )
                else:
                    if param.grad is not None:
                        raise ValueError(
                            f"gradient should be None, but is {param.grad}"
                        )
                    param.grad = paddle.zeros_like(param, dtype=param.dtype)
        else:
            for param_group in self._inner_opt._param_groups:
                for param in param_group['params']:
                    if param.stop_gradient:
                        continue
                    if hasattr(param, "main_grad"):
                        if param.main_grad is not None:
                            raise ValueError(
                                f"gradient should be None, but is {param.main_grad}"
                            )
                        param.main_grad = paddle.zeros_like(
                            param, dtype=paddle.float32
                        )
                    else:
                        if param.grad is not None:
                            raise ValueError(
                                f"gradient should be None, but is {param.grad}"
                            )
                        param.grad = paddle.zeros_like(param, dtype=param.dtype)
        self.step()
        # clear the parameter gradient
        self._inner_opt.clear_grad(set_to_zero=False)

        return self._inner_opt.state_dict()

    def __getattr__(self, item):
        return getattr(self._inner_opt, item)


def shard_optimizer(optimizer, shard_fn=None):
    """

    Warp the global view optimizer to distributed view.

    Note:
        The `shard_fn` should have the following signature:
            def shard_fn(accumulator_name, param, accumulator) -> sharded_accumulator

    Args:
        optimizer (paddle.optimizer.Optimizer): The optimizer to be sharded.
        shard_fn (Callable, optional): The function to shard accumulators. If not specified,
           we simply pass down the dist attr of the params.

    Returns:
        An optimizer with distributed view.

    Examples:
        .. code-block:: python

            >>> import paddle
            >>> import paddle.distributed as dist
            >>> mesh = dist.ProcessMesh([0, 1], dim_names=["x"])
            >>> class MLP(paddle.nn.Layer):
            ...     def __init__(self):
            ...         super().__init__()
            ...         self.fc1 = paddle.nn.Linear(8, 8)
            ...         self.fc2 = paddle.nn.Linear(8, 8)
            ...
            ...     def forward(self, input):
            ...         return self.fc2(self.fc1(input))
            >>> layer = MLP()
            >>> batch = paddle.rand(shape=[8, 8])
            >>> opt = paddle.optimizer.AdamW(parameters=layer.parameters())
            >>> opt = dist.shard_optimizer(opt)
            >>> for _ in range(5):
            >>>     loss = layer(batch)
            >>>     loss.backward()
            >>>     opt.step()
            >>>     opt.clear_grad()
            >>> # This case need to be executed in multi-card environment
            >>> # python -m paddle.distributed.launch --gpus=0,1 {test_case}.py

    """
    return _ShardOptimizer(optimizer, shard_fn)


# Part4: Convert To Static Graph related APIs
class FusePasses:
    """
    A helper class for users to configure the fuse passes.
    """

    def __init__(self, config_dict=None):
        self.enable = False
        self.gemm_epilogue = False
        self.dropout_add = False
        if config_dict is not None:
            for key, value in config_dict.items():
                if hasattr(self, key):
                    setattr(self, key, value)
                else:
                    raise ValueError(f"Unknown fuse pass {key}")


class Strategy(auto_strategy.BaseConfig):
    """
    The `Strategy` object is used to configure the parallelization
    and optimization strategies for static graph. Currently supports
    configuring ``sharding``, ``fused_passes``, ``gradient_merge``
    and ``pipeline``. More strategies will be supported in the future.

    ``sharding`` is used to configure the sharding states of the optimizer,
    for saving the GPU memory.

    ``fused_passes`` is used to configure the fusion of the computation in
    the model.

    ``gradient_merge`` is used to configure the gradient merge strategy in
    training.

    ``pipeline`` is used to configure the pipeline parallelism strategy.

    Args:
        config(dict|None, optional): The user-defined configurations.
            If ``config`` is None, use default configurations. If it is
            a dict, the items inside the dict will be used to set the
            configurations, and the others remain the default values.

    Examples:
        .. code-block:: python

            >>> import paddle
            >>> import paddle.distributed as dist

            >>> strategy = dist.Strategy()

            >>> strategy.sharding.enable = True
            >>> strategy.sharding.stage = 2
            >>> strategy.sharding.degree = 2

            >>> strategy.gradient_merge.enable = True
            >>> strategy.gradient_merge.k_steps = 2
            >>> strategy.gradient_merge.avg = False

            >>> strategy.pipeline.enable = True
            >>> strategy.pipeline.schedule_mode = "1F1B" # default is "1F1B"
            >>> strategy.pipeline.micro_batch_size = 2
    """

    def __init__(self, config=None):
        if config is not None:
            if isinstance(config, dict):
                self._config_dict = copy.deepcopy(config)
            else:
                raise ValueError(
                    f"Expected a dictionary. But received: {config}"
                )
        else:
            self._config_dict = {}

        category = auto_strategy.constants.BASE
        super().__init__(category, self._config_dict)

        config_dict = self._config_dict.get(
            auto_strategy.constants.SHARDING, None
        )
        self._sharding = auto_strategy.ShardingConfig(config_dict)

        config_dict = self._config_dict.get(
            auto_strategy.constants.GRADIENT_MERGE, None
        )
        self._gradient_merge = auto_strategy.GradientMergeConfig(config_dict)

        config_dict = self._config_dict.get(
            auto_strategy.constants.PIPELINE, None
        )
        self._pipeline = auto_strategy.PipelineConfig(config_dict)

        config_dict = self._config_dict.get(
            auto_strategy.constants.FUSED_PASSES, None
        )
        self._fused_passes = FusePasses(config_dict)

    @property
    def sharding(self):
        """
        ``sharding`` is used to configure the sharding states of the optimizer,
        containing following configs:

            ``enable`` (bool): whether to enable sharding. Default: False.

            ``stage`` (int): can be set to 1, 2 or 3. 1 indicates the optimizer states segmentation,
            2 indicates optimizer states and gradient segmentation, 3 indicates the segmentation
            of optimizer states, gradient and parameters. Default: 1.

            ``degree`` (int): the number of segmentation pieces. Default: 8.

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> import paddle.distributed as dist

                >>> strategy = dist.Strategy()

                >>> strategy.sharding.enable = True
                >>> strategy.sharding.stage = 2
                >>> strategy.sharding.degree = 2
        """
        return self._sharding

    @property
    def gradient_merge(self):
        """
        ``gradient_merge`` is used to configure the gradient merge strategy in
        training, containing following configs:

            ``enable`` (bool): whether to enable gradient merge. Default: False.

            ``k_steps`` (int): the number of steps for merging gradients. Default: 1.

            ``avg`` (bool): whether to average the gradients of each step. Default: True.

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> import paddle.distributed as dist

                >>> strategy = dist.Strategy()

                >>> strategy.gradient_merge.enable = True
                >>> strategy.gradient_merge.k_steps = 2
                >>> strategy.gradient_merge.avg = True
        """
        return self._gradient_merge

    @property
    def fused_passes(self):
        """
        ``fused_passes`` is used to configure the fusion of the computation in
        the model, containing following configs:

            ``enable`` (bool): whether to enable fused passes. Default: False.

            ``gemm_epilogue`` (bool): whether to fuse ``matmul`` and ``add`` computation
            in the ``Linear`` layer. Default: False

            "dropout_add" (bool): whether to fuse ``dropout`` and ``add`` computation. Default: False.

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> import paddle.distributed as dist

                >>> strategy = dist.Strategy()

                >>> strategy.fused_passes.enable = True
                >>> strategy.fused_passes.gemm_spilogue = True
                >>> strategy.fused_passes.dropout_add = True
        """
        return self._fused_passes

    @property
    def pipeline(self):
        """
        ``pipeline`` is used to configure the pipeline parallelism,
        containing following configs:

            ``enable`` (bool): whether to enable pipeline parallelism. Default: False.

            ``schedule_mode`` (str): the scheduling mode of pipeline parallelism. Default: "1F1B".

            ``micro_batch_size`` (int): the size of each micro-batch inside a mini-batch. Default: 1.

            ``accumulate_steps`` (int): number of steps for accumulating. Default: 1.

        Examples:
            .. code-block:: python

                >>> import paddle
                >>> import paddle.distributed as dist

                >>> strategy = dist.Strategy()

                >>> strategy.pipeline.enable = True
                >>> strategy.pipeline.micro_batch_size = 2
        """
        return self._pipeline


class DistModel:
    """
    `DistModel` is the model converted from a ``paddle.nn.layer`` with distributed
    tensors as its parameters. It contains the static graph converted from a
    ``paddle.nn.layer`` whose parameters are distributed tensors (constructed
    from ``paddle.distributed.shard_tensor``), and provides the APIs for training,
    evaluation and prediction with the static graph.

    It is suggested to generate DistModel by ``paddle.distributed.to_static``,
    not directly by ``paddle.distributed.DistModel``.

    Please first set the DistModel to "train", "eval" or "predict" mode with
    ``train()/eval()/predict()`` method and then use the ``__call__`` method for
    training, evaluation and prediction respectively.

    For more details of the usage, please refer to the sample code in
    ``paddle.distributed.to_static``.

    Args:
        layer(paddle.nn.Layer): The layer in dygraph mode, whose parameters
            are distributed tensors generated by ``shard_tensor``.
        loader(paddle.io.DataLoader): The data loader used in dygraph mode,
            used to generate DistributedDataloader for training with the
            static graph.
        loss(Loss|Callable|None, optional): The loss function for training
            or evaluating the model. Can be a `paddle.nn.Layer` instance or
            any callable function. If loss is not None, DistModel will be set
            to "train" (when the optimizer is also not None) or "eval" mode
            (when optimizer is None) in default. If it is None, DistModel will
            be set to "predict" mode in default. Default: None.
        optimizer(paddle.optimizer.Optimizer|None, optional): The optimizer
            for training. If both optimizer and loss are set, DistModel will
            be set to "train" mode in default. Default: None.
        strategy(paddle.distributed.Strategy|None, optional): Configs for
            parallel strategies and optimization settings (e.g. sharding,
            pipeline parallelism). Default: None.
    """

    def __init__(
        self,
        layer,
        loader,
        loss=None,
        optimizer=None,
        strategy=None,
        metrics=None,
    ):
        self._feed_name_list = []
        self._inner_strategy = self.__convert_strategy(strategy)
        self._engine = Engine(
            layer, loss, optimizer, metrics, strategy=self._inner_strategy
        )
        self._mode = None
        self._feed_name_list = {}

        # convert dygraph model to static model
        batch_size = loader.batch_sampler.batch_size
        inputs_spec, labels_spec = self._engine._prepare_data_spec(
            loader.dataset, None, batch_size
        )

        if optimizer is not None and loss is not None:
            # get the static graph in train mode
            self._engine.prepare(
                inputs_spec, labels_spec, mode="train", init_parameters=False
            )
        if loss is not None:
            # get the static graph in eval mode
            self._engine.prepare(
                inputs_spec, labels_spec, mode="eval", init_parameters=False
            )
        # get the static graph in predict mode
        self._engine.prepare(
            inputs_spec, None, mode="predict", init_parameters=False
        )

        # set the default mode
        if optimizer is not None and loss is not None:
            self.train()
        elif loss is not None:
            self.eval()
        else:
            self.predict()

        # get DistributedDataLoader for static mode auto-parallelism
        batch_size = self._engine._validate_batch_size(batch_size)
        self.dist_loader = self._engine._prepare_dataloader(
            loader.dataset, return_list=True, batch_size=batch_size
        )

    def train(self):
        """
        Set the DistModel to "train" mode. In "train" mode,
        executing ``__call__`` method will update the
        parameters of the model and return the loss.
        """
        if not self._engine._has_prepared["train"]:
            raise RuntimeError(
                "The model for training has not been prepared, please set 'loss' and 'optimizer' when constructing DistModel."
            )
        self._mode = "train"
        self._engine.to_mode("train")

    def eval(self):
        """
        Set the mode of DistModel to "eval". In "eval" mode,
        executing ``__call__`` will return the loss.
        """
        if not self._engine._has_prepared["eval"]:
            raise RuntimeError(
                "The model for evaluation has not been prepared, please set 'loss' when constructing DistModel."
            )
        self._mode = "eval"
        self._engine.to_mode("eval")

    def predict(self):
        """
        Set the mode of DistModel to "predict". In "predict" mode,
        executing ``__call__`` returns a dict that contains the
        outputs of the model.
        """
        if not self._engine._has_prepared["predict"]:
            raise RuntimeError(
                "The model for prediction has not been prepared."
            )
        self._mode = "predict"
        self._engine.to_mode("predict")

    def __validate_mode(self, mode):
        if mode is None and self._mode is None:
            raise ValueError(
                "Please set the mode or call train()/eval()/predict() first."
            )
        if mode is None:
            mode = self._mode
        if mode not in ["train", "eval", "predict"]:
            raise ValueError("mode can only be 'train', 'eval' or 'predict'.")
        return mode

    def dist_main_program(self, mode=None):
        """
        Get the distributed main program of the specified ``mode``. Each
        'mode' has its own distributed main program, ``dist_main_program``
        returns the corresponding distributed main program of ``mode``.

        Args:
            mode (str|None, optional): Can be 'train' , 'eval' , 'predict' or None.
                'train' : Return the distributed main program for training.
                'eval' : Return the distributed main program for evaluation.
                'predict' : Return the distributed main program for prediction.
                None : The current mode of the DistModel will be used.
                Default : None.

        Returns:
            The distributed main program of ``mode``.
        """
        mode = self.__validate_mode(mode)
        return self._engine.get_dist_main_program(mode)

    def dist_startup_program(self, mode=None):
        """
        Get the corresponding distributed startup program of ``mode``,
        which is used for initializing the parameters.

        Args:
            mode (str|None, optional): Can be 'train' , 'eval' , 'predict' or None.
                'train' : Return the distributed startup program for training.
                'eval' : Return the distributed startup program for evaluation.
                'predict' : Return the distributed startup program for prediction.
                None: The current mode of the DistModel will be used.
                Default : None.

        Returns:
            The distributed startup program of ``mode``.
        """
        mode = self.__validate_mode(mode)
        return self._engine.get_dist_startup_program(mode)

    def serial_main_program(self, mode=None):
        """
        Get the corresponding serial main program of ``mode``, containing
        the whole variables and operators of the given ``layer``.

        Args:
            mode (str|None, optional): Can be 'train', 'eval', 'predict' or None.
                'train' : Return the main program for training.
                'eval' : Return the main program for evaluation.
                'predict' : Return the main program for prediction.
                None : The current mode of the DistModel will be used.
                Default : None.

        Returns:
            The serial main program of ``mode``.
        """
        mode = self.__validate_mode(mode)
        return self._engine.get_serial_main_program(mode)

    def serial_startup_program(self, mode=None):
        """
        Get the corresponding serial startup program of ``mode``.

        Args:
            mode (str|None, optional): Can be 'train' , 'eval' , 'predict' or None.
                'train' : Return the serial startup program for training.
                'eval' : Return the serial startup program for evaluation.
                'predict' : Return the serial startup program for prediction.
                None : The current mode of the DistModel will be used.
                Default : None.

        Returns:
            The serial startup program of ``mode``.
        """
        mode = self.__validate_mode(mode)
        return self._engine.get_serial_startup_program(mode)

    def _make_feeds(self, data_list):
        if (
            self._mode not in self._feed_name_list
            or self._feed_name_list[self._mode] == []
        ):
            feed_list = self._engine.get_feed_list()
            self._feed_name_list[self._mode] = [var.name for var in feed_list]
        feed_name_list = self._feed_name_list[self._mode]
        if len(feed_name_list) != len(data_list):
            raise ValueError(
                "The input data and feed_list are not consistent."
                "The model takes %s as input" % (str(feed_name_list))
            )
        return dict(zip(feed_name_list, data_list))

    def __convert_strategy(self, strategy):
        import copy

        if strategy is None:
            return None
        inner_strategy = auto_strategy.Strategy()
        inner_strategy.fused_passes.enable = strategy.fused_passes.enable
        if strategy.fused_passes.gemm_epilogue is True:
            inner_strategy.fused_passes.fused_passes_list.append(
                "fused_gemm_epilogue_pass"
            )
        if strategy.fused_passes.dropout_add is True:
            inner_strategy.fused_passes.fused_passes_list.append(
                "fused_dropout_add_pass"
            )

        inner_strategy.sharding = copy.deepcopy(strategy.sharding)
        inner_strategy.gradient_merge = copy.deepcopy(strategy.gradient_merge)
        inner_strategy.pipeline = copy.deepcopy(strategy.pipeline)
        return inner_strategy

    def __call__(self, *args):
        if self._mode is None:
            raise ValueError("Please call train()/eval()/predict() first.")
        if self._mode == "train":
            if self._engine._optimizer is None or self._engine._loss is None:
                raise ValueError(
                    "Please set optimizer and loss function before training."
                )
        if self._mode == "eval":
            if self._engine._loss is None:
                raise ValueError("Please set loss function before evaluation.")
        feeds = self._make_feeds(list(args))
        outs = self._engine.run(feeds)

        if self._mode == "predict":
            if "outputs" in outs:
                return outs["outputs"]
            else:
                return None
        else:
            if "loss" in outs:
                return outs["loss"]
            else:
                return None

    def state_dict(self, mode="all"):
        """
        Get the state dict of model and optimizer.

        Args:
            mode (str): Can be ['opt', 'param', 'all'],
                'opt' :  The return value only contains the variable in the optimizer.
                'param' : The return value only contains the variable in the network, not the variable in the optimizer.
                'all' : The return value contains the variable in the network and optimizer.
                Default: 'all'
        """
        local_state_dict = self.dist_main_program(
            mode=self._engine._mode
        ).state_dict(mode)
        dist_state_dict = self._build_distributed_state_dict(local_state_dict)
        return dist_state_dict

    def _build_distributed_state_dict(self, local_state_dict):
        """
        Args:
            local_state_dict(Dict[str, libpaddle.Tensor]): The state dict from program.
        """
        dist_main_program = self.dist_main_program(mode=self._engine._mode)
        dist_context = self._engine._dist_contexts[self._mode]
        # Dict[var.name, Dict["process_shape": process_mesh.shape, "process_group": process_mesh.process_ids, "dims_mapping": dims_mapping]]
        dist_attrs = get_dist_attr(dist_main_program, dist_context)

        def build_distributed_tensor(local_tensor, dist_attr):
            assert isinstance(
                local_tensor, (paddle.Tensor, np.ndarray, paddle.base.Tensor)
            )
            if not isinstance(local_tensor, paddle.Tensor):
                local_tensor = paddle.Tensor(local_tensor)
            assert isinstance(
                local_tensor, paddle.Tensor
            ), f"local tensor:{local_tensor} type {type(local_tensor)} is not paddle.Tensor."
            assert len(local_tensor.shape) == len(
                dist_attr["dims_mapping"]
            ), f"local tensor shape {local_tensor.shape} not equal to dims_mapping shape {dist_attr['dims_mapping']}."
            global_shape = local_tensor.shape
            for i, dim in enumerate(dist_attr["dims_mapping"]):
                assert dim >= -1 and dim < len(
                    local_tensor.shape
                ), f"dim {dim} out of range."
                if dim == -1:
                    continue
                elif dim >= 0:
                    global_shape[i] = (
                        dist_attr["process_shape"][dim] * local_tensor.shape[i]
                    )
                else:
                    raise ValueError(f"dim {dim} is not supported.")
            # TODO(pangengzheng): construct dist_tensor with _dtensor_from_local api when it is ready.
            global_tensor = paddle.zeros(global_shape, dtype=local_tensor.dtype)
            mesh = dist.ProcessMesh(
                np.array(dist_attr["process_group"]).reshape(
                    dist_attr["process_shape"]
                )
            )
            placements = to_placements(dist_attr["dims_mapping"], mesh)
            dist_tensor = dist.shard_tensor(global_tensor, mesh, placements)
            assert (
                dist_tensor._local_value().shape == local_tensor.shape
            ), f"local tensor shape {dist_tensor._local_value().shape} not equal to local_tensor.shape:{local_tensor.shape}"
            paddle.assign(local_tensor, dist_tensor._local_value())
            return dist_tensor

        global_state_dict = {}
        with paddle.base.dygraph.guard():
            for var_name, tensor in local_state_dict.items():
                assert (
                    var_name in dist_attrs
                ), f"var {var_name} not in dist attrs:{dist_attrs}."
                global_state_dict[var_name] = build_distributed_tensor(
                    tensor, dist_attrs[var_name]
                )
        return global_state_dict

    def set_state_dict(self, state_dict):
        local_state_dict = {}
        dist_main_program = self.dist_main_program(mode=self._engine._mode)
        cur_state_dict = self.state_dict()
        for k, v in state_dict.items():
            assert v.is_dist(), f"key {k} value:{v} is not a dist tensor."
            if k in cur_state_dict:
                cur_v = cur_state_dict[k]
                assert v.process_mesh == cur_state_dict[
                    k
                ].process_mesh or check_placements_equal(
                    v.placements, cur_v.placements
                ), f"process_mesh:{v.process_mesh} != {cur_v.process_mesh} or placements:{v.placements} != {cur_v.placements} not match"
            local_state_dict[k] = v._local_value()
        dist_main_program.set_state_dict(local_state_dict)


def to_static(
    layer: paddle.nn.Layer,
    loader=None,
    loss=None,
    optimizer=None,
    strategy=None,
):
    """
    Converts the ``layer`` with distributed tensor (constructed from
    ``paddle.distributed.shard_tensor``) to a static graph. ``to_static``
    returns a DistModel instance containing the static graph for
    distributed training, evaluation and prediction, and an object of
    DistributedDataLoader to generate data.

    Args:
        layer(paddle.nn.Layer): The layer in dygraph mode, the parameters
            or its inputs can be distributed tensors.
        loader(paddle.io.DataLoader): The data loader used in dygraph mode,
            used to generate DistributedDataloader.
        loss(Loss|Callable|None, optional): The loss function for training
            or evaluating the model. Can be a `paddle.nn.Layer` instance or
            any callable function. Default: None.
        optimizer(paddle.optimizer.Optimizer|_ShardOptimizer|None, optional):
            The optimizer for training. It can `paddle.optimizer.Optimizer`
            or `_ShardOptimizer` wrapped by `shard_optimizer`. Default: None.
        strategy(paddle.distributed.Strategy|None, optional): Configs for
            parallel strategies and optimization settings (e.g. sharding,
            pipeline parallelism). Default: None.

    Returns:
        DistModel: A ``DistModel`` instance converted the input ``layer``.
        DistributedDataLoader: An optimized data loader that can be used to generate data, it's usage is the same as ``paddle.io.DataLoader``.

    Examples:
        .. code-block:: python

            >>> import numpy as np
            >>> import paddle
            >>> import paddle.distributed as dist
            >>> from paddle import nn
            >>> from paddle.distributed import Replicate, Shard

            >>> BATCH_SIZE = 4
            >>> BATCH_NUM = 4
            >>> IMAGE_SIZE = 16
            >>> CLASS_NUM = 8
            >>> class RandomDataset(paddle.io.Dataset):
            ...     def __init__(self, images, labels, num_samples):
            ...         self.images = images
            ...         self.labels = labels
            ...         self.num_samples = num_samples
            ...     def __getitem__(self, idx):
            ...         return self.images[idx], self.labels[idx]
            ...     def __len__(self):
            ...         return self.num_samples

            >>> class DemoNet(nn.Layer):
            ...     def __init__(self, mesh):
            ...         super().__init__()
            ...         self._mesh = mesh
            ...         self.linear_0 = nn.Linear(IMAGE_SIZE, IMAGE_SIZE)
            ...         self.linear_1 = nn.Linear(IMAGE_SIZE, CLASS_NUM)
            ...         self.relu = nn.ReLU()
            ...         # shard the weights of this layer
            ...         self.linear_0.weight = dist.shard_tensor(
            ...             self.linear_0.weight,
            ...             self._mesh,
            ...             [Shard(1)],
            ...             stop_gradient=False,
            ...         )
            ...         self.linear_1.weight = dist.shard_tensor(
            ...             self.linear_1.weight,
            ...             self._mesh,
            ...             [Shard(0)],
            ...             stop_gradient=False,
            ...         )
            ...     def forward(self, x):
            ...         out = self.linear_0(x)
            ...         out = self.relu(out)
            ...         out = self.linear_1(out)
            ...         return out

            >>> # doctest: +REQUIRES(env:DISTRIBUTED)
            >>> images = np.random.rand(BATCH_SIZE, IMAGE_SIZE).astype('float32')
            >>> labels = np.random.rand(BATCH_SIZE, CLASS_NUM).astype('float32')
            >>> dataset = RandomDataset(images, labels, BATCH_SIZE)
            >>> loader = paddle.io.DataLoader(dataset, batch_size=BATCH_SIZE)

            >>> mesh = dist.ProcessMesh([0, 1], dim_names=["x"])
            >>> layer = DemoNet(mesh)
            >>> opt = paddle.optimizer.SGD(
            ...     learning_rate=0.1, parameters=layer.parameters()
            ... )
            >>> loss_fn = nn.MSELoss()

            >>> dist_model, dist_loader = dist.to_static(
            ...     layer, loader, loss_fn, opt
            ... )

            >>> # training
            >>> dist_model.train()
            >>> for batch_id, (image, label) in enumerate(dist_loader()):
            ...     # in train mode, executing the __call__ method will
            ...     # update the parameters of the model and return the
            ...     # loss
            ...     loss = dist_model(image, label)

            >>> # evaluation
            >>> dist_model.eval()
            >>> for batch_id, (image, label) in enumerate(dist_loader()):
            ...     # in eval mode, executing the __call__ method will
            ...     # return the loss
            ...     loss = dist_model(image, label)

            >>> # prediction
            >>> dist_model.predict()
            >>> for batch_id, (image, label) in enumerate(dist_loader()):
            ...     # in predict mode, executing the __call__ method will
            ...     # return a dict that contains the outputs of the model,
            ...     # where the value of "out0" is the first output.
            ...     outs = dist_model(image)

            >>> # This case need to be executed in multi-card environment
            >>> # export CUDA_VISIBLE_DEVICES=0,1
            >>> # python -m paddle.distributed.launch {test_case}.py
    """
    if isinstance(optimizer, _ShardOptimizer):
        optimizer = optimizer._inner_opt

    dist_model = DistModel(layer, loader, loss, optimizer, strategy)
    dist_loader = dist_model.dist_loader

    return dist_model, dist_loader
