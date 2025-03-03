#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

import re
import warnings

from paddle.utils import gast

from .base_transformer import BaseTransformer
from .utils import RE_PYMODULE, RE_PYNAME, ast_to_source_code

__all__ = []

IGNORE_NAMES = [
    'declarative',
    'to_static',
    'wraps',
    'staticmethod',
    'classmethod',
    'decorator',
]


class DecoratorTransformer(BaseTransformer):
    """
    Transform decorators.
    """

    def __init__(self, root):
        self.root = root

        self.ancestor_nodes = []

    def transform(self):
        """
        Main function to transform AST.
        """
        self.visit(self.root)

    def visit_FunctionDef(self, node):
        assert isinstance(node, gast.FunctionDef)
        self.generic_visit(node)

        deco_list = node.decorator_list
        node.decorator_list = []

        # every decorator will append a node
        decofun_nodes = []
        # func to be decoed next time
        deco_target = '_orig_' + node.name
        # last decoed func
        decoed_func = ''

        for deco in reversed(deco_list):
            # skip INGNORE_NAMES
            deco_full_name = ast_to_source_code(deco).strip()
            if isinstance(deco, gast.Call):
                # match case like :
                # 1: @_jst.Call(a.b.c.d.deco)()
                # 2: @q.w.e.r.deco()
                re_tmp = re.match(
                    r'({module})*({name}\(){{0,1}}({module})*({name})(\)){{0,1}}\(.*$'.format(
                        name=RE_PYNAME, module=RE_PYMODULE
                    ),
                    deco_full_name,
                )
                deco_name = re_tmp.group(4)
            else:
                # match case like:
                # @a.d.g.deco
                re_tmp = re.match(
                    fr'({RE_PYMODULE})*({RE_PYNAME})$',
                    deco_full_name,
                )
                deco_name = re_tmp.group(2)
            if deco_name in IGNORE_NAMES:
                continue
            elif deco_name == 'contextmanager':
                warnings.warn(
                    "Dy2Static : A context manager decorator is used, this may not work correctly after transform."
                )

            decoed_func = '_decoedby_' + deco_name

            # get function after decoration
            if isinstance(deco, gast.Call):
                if '_jst.Call' in deco_full_name:
                    # in this case , the deco_full_name will be like:
                    # '_jst.Call(deco)(5)'
                    rematch = re.match(
                        r'\_jst\.Call\((.+?)\)\((.*)\)', deco_full_name
                    )
                    re_name = rematch.group(1)
                    re_args = rematch.group(2)
                    re_args_with_func = deco_target + ', ' + re_args
                    decofun_str = 'try:\n\t{0} = _jst.Call({1})({2})\nexcept:\n\t{0} = _jst.Call({1})({3})({4})'.format(
                        decoed_func,
                        re_name,
                        re_args_with_func,
                        re_args,
                        deco_target,
                    )
                else:
                    # paddle api will not be transformed to '_jst.Call'
                    rematch = re.match(r'(.+?)\((.*)\)', deco_full_name)
                    re_name = rematch.group(1)
                    re_args = rematch.group(2)
                    re_args_with_func = deco_target + ', ' + re_args
                    decofun_str = 'try:\n\t{0} = {1}({2})\nexcept:\n\t{0} = {1}({3})({4})'.format(
                        decoed_func,
                        re_name,
                        re_args_with_func,
                        re_args,
                        deco_target,
                    )

            else:
                decofun_str = '{} = _jst.Call({})({})'.format(
                    decoed_func, deco_full_name, deco_target
                )

            decofun_nodes.extend(gast.parse(decofun_str).body)
            deco_target = decoed_func

        if not decofun_nodes:
            return node

        orig_func_node = gast.FunctionDef(
            name='_orig_' + node.name,
            args=node.args,
            body=node.body,
            decorator_list=[],
            returns=None,
            type_comment=None,
        )

        args = [arg.id for arg in node.args.args]
        arg_str = ','.join(args)
        callfun_str = f'return {decoed_func}({arg_str})'
        callfun_node = gast.parse(callfun_str).body[0]

        node.body = [orig_func_node] + decofun_nodes + [callfun_node]

        return node
