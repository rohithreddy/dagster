from abc import ABCMeta, abstractmethod
from collections import defaultdict, namedtuple

import six

from dagster import check
from dagster.core.errors import DagsterInvalidDefinitionError
from dagster.serdes import whitelist_for_serdes
from dagster.utils import frozentags

from .hook import HookDefinition
from .input import InputDefinition
from .output import OutputDefinition
from .utils import DEFAULT_OUTPUT, struct_to_string, validate_tags


class SolidInvocation(namedtuple('Solid', 'name alias tags hook_defs')):
    '''Identifies an instance of a solid in a pipeline dependency structure.

    Args:
        name (str): Name of the solid of which this is an instance.
        alias (Optional[str]): Name specific to this instance of the solid. Necessary when there are
            multiple instances of the same solid.
        tags (Optional[Dict[str, Any]]): Optional tags values to extend or override those
            set on the solid definition.
        hook_defs (Optional[Set[HookDefinition]]): A set of hook definitions applied to the
            solid instance.

    Examples:

        .. code-block:: python

            pipeline = PipelineDefinition(
                solid_defs=[solid_1, solid_2]
                dependencies={
                    SolidInvocation('solid_1', alias='other_name') : {
                        'input_name' : DependencyDefinition('solid_1'),
                    },
                    'solid_2' : {
                        'input_name': DependencyDefinition('other_name'),
                    },
                }
            )

    In general, users should prefer not to construct this class directly or use the
    :py:class:`PipelineDefinition` API that requires instances of this class. Instead, use the
    :py:func:`@pipeline <pipeline>` API:

    .. code-block:: python

        @pipeline
        def pipeline():
            other_name = solid_1.alias('other_name')
            solid_2(other_name(solid_1))

    '''

    def __new__(cls, name, alias=None, tags=None, hook_defs=None):
        name = check.str_param(name, 'name')
        alias = check.opt_str_param(alias, 'alias')
        tags = frozentags(check.opt_dict_param(tags, 'tags', value_type=str, key_type=str))
        hook_defs = frozenset(check.opt_set_param(hook_defs, 'hook_defs', of_type=HookDefinition))
        return super(cls, SolidInvocation).__new__(cls, name, alias, tags, hook_defs)


class Solid(object):
    '''
    Solid invocation within a pipeline. Defined by its name inside the pipeline.

    Attributes:
        name (str):
            Name of the solid inside the pipeline. Must be unique per-pipeline.
        definition (SolidDefinition):
            Definition of the solid.
    '''

    def __init__(self, name, definition, container_definition=None, tags=None, hook_defs=None):
        from .solid import ISolidDefinition, CompositeSolidDefinition

        self.name = check.str_param(name, 'name')
        self.definition = check.inst_param(definition, 'definition', ISolidDefinition)
        self.container_definition = check.opt_inst_param(
            container_definition, 'container_definition', CompositeSolidDefinition
        )
        self._additional_tags = validate_tags(tags)
        self._hook_defs = check.opt_set_param(hook_defs, 'hook_defs', of_type=HookDefinition)

        input_handles = {}
        for name, input_def in self.definition.input_dict.items():
            input_handles[name] = SolidInputHandle(self, input_def)

        self._input_handles = input_handles

        output_handles = {}
        for name, output_def in self.definition.output_dict.items():
            output_handles[name] = SolidOutputHandle(self, output_def)

        self._output_handles = output_handles

    def input_handles(self):
        return self._input_handles.values()

    def output_handles(self):
        return self._output_handles.values()

    def input_handle(self, name):
        check.str_param(name, 'name')
        return self._input_handles[name]

    def output_handle(self, name):
        check.str_param(name, 'name')
        return self._output_handles[name]

    def has_input(self, name):
        return self.definition.has_input(name)

    def input_def_named(self, name):
        return self.definition.input_def_named(name)

    def has_output(self, name):
        return self.definition.has_output(name)

    def output_def_named(self, name):
        return self.definition.output_def_named(name)

    @property
    def is_composite(self):
        from .solid import CompositeSolidDefinition

        return isinstance(self.definition, CompositeSolidDefinition)

    @property
    def input_dict(self):
        return self.definition.input_dict

    @property
    def output_dict(self):
        return self.definition.output_dict

    @property
    def tags(self):
        return self.definition.tags.updated_with(self._additional_tags)

    def container_maps_input(self, input_name):
        if self.container_definition is None:
            return False

        return self.container_definition.mapped_input(self.name, input_name) is not None

    def container_mapped_input(self, input_name):
        return self.container_definition.mapped_input(self.name, input_name)

    @property
    def hook_defs(self):
        return self._hook_defs


@whitelist_for_serdes
class SolidHandle(namedtuple('_SolidHandle', 'name parent')):
    def __new__(cls, name, parent):
        return super(SolidHandle, cls).__new__(
            cls, check.str_param(name, 'name'), check.opt_inst_param(parent, 'parent', SolidHandle),
        )

    def __str__(self):
        return self.to_string()

    @property
    def path(self):
        '''Return a list representation of the handle.

        Inverse of SolidHandle.from_path.

        Returns:
            List[str]:
        '''
        path = []
        cur = self
        while cur:
            path.append(cur.name)
            cur = cur.parent
        path.reverse()
        return path

    def to_string(self):
        '''Return a unique string representation of the handle.

        Inverse of SolidHandle.from_string.
        '''
        return self.parent.to_string() + '.' + self.name if self.parent else self.name

    def is_or_descends_from(self, handle):
        '''Check if the handle is or descends from another handle.

        Args:
            handle (SolidHandle): The handle to check against.

        Returns:
            bool:
        '''
        check.inst_param(handle, 'handle', SolidHandle)

        for idx in range(len(handle.path)):
            if idx >= len(self.path):
                return False
            if self.path[idx] != handle.path[idx]:
                return False
        return True

    def pop(self, ancestor):
        '''Return a copy of the handle with some of its ancestors pruned.

        Args:
            ancestor (SolidHandle): Handle to an ancestor of the current handle.

        Returns:
            SolidHandle:

        Example:

        .. code-block:: python

            handle = SolidHandle('baz', SolidHandle('bar', SolidHandle('foo', None)))
            ancestor = SolidHandle('bar', SolidHandle('foo', None))
            assert handle.pop(ancestor) == SolidHandle('baz', None)
        '''

        check.inst_param(ancestor, 'ancestor', SolidHandle)
        check.invariant(
            self.is_or_descends_from(ancestor),
            'Handle {handle} does not descend from {ancestor}'.format(
                handle=self.to_string(), ancestor=ancestor.to_string()
            ),
        )

        return SolidHandle.from_path(self.path[len(ancestor.path) :])

    def with_ancestor(self, ancestor):
        '''Returns a copy of the handle with an ancestor grafted on.

        Args:
            ancestor (SolidHandle): Handle to the new ancestor.

        Returns:
            SolidHandle:

        Example:

        .. code-block:: python

            handle = SolidHandle('baz', SolidHandle('bar', SolidHandle('foo', None)))
            ancestor = SolidHandle('quux' None)
            assert handle.with_ancestor(ancestor) == SolidHandle(
                'baz', SolidHandle('bar', SolidHandle('foo', SolidHandle('quux', None)))
            )
        '''
        check.opt_inst_param(ancestor, 'ancestor', SolidHandle)

        return SolidHandle.from_path((ancestor.path if ancestor else []) + self.path)

    @staticmethod
    def from_path(path):
        check.list_param(path, 'path', of_type=str)

        cur = None
        while len(path) > 0:
            cur = SolidHandle(name=path.pop(0), parent=cur)
        return cur

    @staticmethod
    def from_string(handle_str):
        check.str_param(handle_str, 'handle_str')

        path = handle_str.split('.')
        return SolidHandle.from_path(path)

    @classmethod
    def from_dict(cls, dict_repr):
        '''This method makes it possible to rehydrate a potentially nested SolidHandle after a
        roundtrip through json.loads(json.dumps(SolidHandle._asdict()))'''

        check.dict_param(dict_repr, 'dict_repr', key_type=str)
        check.invariant(
            'name' in dict_repr, 'Dict representation of SolidHandle must have a \'name\' key'
        )
        check.invariant(
            'parent' in dict_repr, 'Dict representation of SolidHandle must have a \'parent\' key'
        )

        if isinstance(dict_repr['parent'], (list, tuple)):
            dict_repr['parent'] = SolidHandle.from_dict(
                {'name': dict_repr['parent'][0], 'parent': dict_repr['parent'][1],}
            )

        return SolidHandle(**{k: dict_repr[k] for k in ['name', 'parent']})


class SolidInputHandle(namedtuple('_SolidInputHandle', 'solid input_def')):
    def __new__(cls, solid, input_def):
        return super(SolidInputHandle, cls).__new__(
            cls,
            check.inst_param(solid, 'solid', Solid),
            check.inst_param(input_def, 'input_def', InputDefinition),
        )

    def _inner_str(self):
        return struct_to_string(
            'SolidInputHandle', solid_name=self.solid.name, input_name=self.input_def.name,
        )

    def __str__(self):
        return self._inner_str()

    def __repr__(self):
        return self._inner_str()

    def __hash__(self):
        return hash((self.solid.name, self.input_def.name))

    def __eq__(self, other):
        return self.solid.name == other.solid.name and self.input_def.name == other.input_def.name

    @property
    def solid_name(self):
        return self.solid.name


class SolidOutputHandle(namedtuple('_SolidOutputHandle', 'solid output_def')):
    def __new__(cls, solid, output_def):
        return super(SolidOutputHandle, cls).__new__(
            cls,
            check.inst_param(solid, 'solid', Solid),
            check.inst_param(output_def, 'output_def', OutputDefinition),
        )

    def _inner_str(self):
        return struct_to_string(
            'SolidOutputHandle', solid_name=self.solid.name, output_name=self.output_def.name,
        )

    def __str__(self):
        return self._inner_str()

    def __repr__(self):
        return self._inner_str()

    def __hash__(self):
        return hash((self.solid.name, self.output_def.name))

    def __eq__(self, other):
        return self.solid.name == other.solid.name and self.output_def.name == other.output_def.name

    @property
    def solid_name(self):
        return self.solid.name


class InputToOutputHandleDict(defaultdict):
    def __init__(self):
        defaultdict.__init__(self, list)

    def __getitem__(self, key):
        check.inst_param(key, 'key', SolidInputHandle)
        return defaultdict.__getitem__(self, key)

    def __setitem__(self, key, val):
        check.inst_param(key, 'key', SolidInputHandle)
        if not (isinstance(val, SolidOutputHandle) or isinstance(val, list)):
            check.failed(
                'Value must be SolidOutoutHandle or List[SolidOutputHandle], got {val}'.format(
                    val=type(val)
                )
            )

        return defaultdict.__setitem__(self, key, val)


def _create_handle_dict(solid_dict, dep_dict):
    check.dict_param(solid_dict, 'solid_dict', key_type=str, value_type=Solid)
    check.two_dim_dict_param(dep_dict, 'dep_dict', value_type=IDependencyDefinition)

    handle_dict = InputToOutputHandleDict()

    for solid_name, input_dict in dep_dict.items():
        from_solid = solid_dict[solid_name]
        for input_name, dep_def in input_dict.items():
            if dep_def.is_multi():
                handle_dict[from_solid.input_handle(input_name)] = [
                    solid_dict[dep.solid].output_handle(dep.output)
                    for dep in dep_def.get_definitions()
                ]
            else:
                handle_dict[from_solid.input_handle(input_name)] = solid_dict[
                    dep_def.solid
                ].output_handle(dep_def.output)

    return handle_dict


class DependencyStructure(object):
    @staticmethod
    def from_definitions(solids, dep_dict):
        return DependencyStructure(list(dep_dict.keys()), _create_handle_dict(solids, dep_dict))

    def __init__(self, solid_names, handle_dict):
        self._solid_names = solid_names
        self._handle_dict = check.inst_param(handle_dict, 'handle_dict', InputToOutputHandleDict)

        # Building up a couple indexes here so that one can look up all the upstream output handles
        # or downstream input handles in O(1). Without this, this can become O(N^2) where N is solid
        # count during the GraphQL query in particular

        # solid_name => input_handle => list[output_handle]
        self._solid_input_index = defaultdict(dict)

        # solid_name => output_handle => list[input_handle]
        self._solid_output_index = defaultdict(lambda: defaultdict(list))

        for input_handle, output_handle_or_list in self._handle_dict.items():
            output_handle_list = (
                output_handle_or_list
                if isinstance(output_handle_or_list, list)
                else [output_handle_or_list]
            )
            self._solid_input_index[input_handle.solid.name][input_handle] = output_handle_list
            for output_handle in output_handle_list:
                self._solid_output_index[output_handle.solid.name][output_handle].append(
                    input_handle
                )

    def all_upstream_outputs_from_solid(self, solid_name):
        check.str_param(solid_name, 'solid_name')

        # flatten out all outputs that feed into the inputs of this solid
        return [
            output_handle
            for output_handle_list in self._solid_input_index[solid_name].values()
            for output_handle in output_handle_list
        ]

    def input_to_upstream_outputs_for_solid(self, solid_name):
        '''
        Returns a Dict[SolidInputHandle, List[SolidOutputHandle]] that encodes
        where all the the inputs are sourced from upstream. Usually the
        List[SolidOutputHandle] will be a list of one, except for the
        multi-dependency case.
        '''
        check.str_param(solid_name, 'solid_name')
        return self._solid_input_index[solid_name]

    def output_to_downstream_inputs_for_solid(self, solid_name):
        '''
        Returns a Dict[SolidOutputHandle, List[SolidInputHandle]] that
        represents all the downstream inputs for each output in the
        dictionary
        '''
        check.str_param(solid_name, 'solid_name')
        return self._solid_output_index[solid_name]

    def has_singular_dep(self, solid_input_handle):
        check.inst_param(solid_input_handle, 'solid_input_handle', SolidInputHandle)
        return isinstance(self._handle_dict.get(solid_input_handle), SolidOutputHandle)

    def get_singular_dep(self, solid_input_handle):
        check.inst_param(solid_input_handle, 'solid_input_handle', SolidInputHandle)
        dep = self._handle_dict[solid_input_handle]
        check.invariant(
            isinstance(dep, SolidOutputHandle),
            'Can not call get_singular_dep when dep is not singular, got {dep}'.format(
                dep=type(dep)
            ),
        )
        return dep

    def has_multi_deps(self, solid_input_handle):
        check.inst_param(solid_input_handle, 'solid_input_handle', SolidInputHandle)
        return isinstance(self._handle_dict.get(solid_input_handle), list)

    def get_multi_deps(self, solid_input_handle):
        check.inst_param(solid_input_handle, 'solid_input_handle', SolidInputHandle)
        dep = self._handle_dict[solid_input_handle]
        check.invariant(
            isinstance(dep, list),
            'Can not call get_multi_dep when dep is singular, got {dep}'.format(dep=type(dep)),
        )
        return dep

    def has_deps(self, solid_input_handle):
        check.inst_param(solid_input_handle, 'solid_input_handle', SolidInputHandle)
        return solid_input_handle in self._handle_dict

    def get_deps_list(self, solid_input_handle):
        check.inst_param(solid_input_handle, 'solid_input_handle', SolidInputHandle)
        check.invariant(self.has_deps(solid_input_handle))
        if self.has_singular_dep(solid_input_handle):
            return [self.get_singular_dep(solid_input_handle)]
        else:
            return self.get_multi_deps(solid_input_handle)

    def input_handles(self):
        return list(self._handle_dict.keys())

    def items(self):
        return self._handle_dict.items()

    def debug_str(self):
        if not self.items():
            return 'DependencyStructure: EMPTY'

        debug = 'DependencyStructure: \n'
        for in_handle, out_handle in self.items():
            debug += '  {out_solid}.{out_name} ---> {in_solid}.{in_name}\n'.format(
                out_solid=out_handle.solid.name,
                out_name=out_handle.output_def.name,
                in_name=in_handle.input_def.name,
                in_solid=in_handle.solid.name,
            )
        return debug


class IDependencyDefinition(six.with_metaclass(ABCMeta)):  # pylint: disable=no-init
    @abstractmethod
    def get_definitions(self):
        pass

    @abstractmethod
    def is_multi(self):
        pass


class DependencyDefinition(
    namedtuple('_DependencyDefinition', 'solid output description'), IDependencyDefinition
):
    '''Represents an edge in the DAG of solid instances forming a pipeline.

    This object is used at the leaves of a dictionary structure that represents the complete
    dependency structure of a pipeline whose keys represent the dependent solid and dependent
    input, so this object only contains information about the dependee.

    Concretely, if the input named 'input' of solid_b depends on the output named 'result' of
    solid_a, this structure will look as follows:

    .. code-block:: python

        dependency_structure = {
            'solid_b': {
                'input': DependencyDefinition('solid_a', 'result')
            }
        }

    In general, users should prefer not to construct this class directly or use the
    :py:class:`PipelineDefinition` API that requires instances of this class. Instead, use the
    :py:func:`@pipeline <pipeline>` API:

    .. code-block:: python

        @pipeline
        def pipeline():
            solid_b(solid_a())


    Args:
        solid (str): The name of the solid that is depended on, that is, from which the value
            passed between the two solids originates.
        output (Optional[str]): The name of the output that is depended on. (default: "result")
        description (Optional[str]): Human-readable description of this dependency.
    '''

    def __new__(cls, solid, output=DEFAULT_OUTPUT, description=None):
        return super(DependencyDefinition, cls).__new__(
            cls,
            check.str_param(solid, 'solid'),
            check.str_param(output, 'output'),
            check.opt_str_param(description, 'description'),
        )

    def get_definitions(self):
        return [self]

    def is_multi(self):
        return False


class MultiDependencyDefinition(
    namedtuple('_MultiDependencyDefinition', 'dependencies'), IDependencyDefinition
):
    '''Represents a fan-in edge in the DAG of solid instances forming a pipeline.

    This object is used only when an input of type ``List[T]`` is assembled by fanning-in multiple
    upstream outputs of type ``T``.

    This object is used at the leaves of a dictionary structure that represents the complete
    dependency structure of a pipeline whose keys represent the dependent solid and dependent
    input, so this object only contains information about the dependee.

    Concretely, if the input named 'input' of solid_c depends on the outputs named 'result' of
    solid_a and solid_b, this structure will look as follows:

    .. code-block:: python

        dependency_structure = {
            'solid_c': {
                'input': MultiDependencyDefinition(
                    [
                        DependencyDefinition('solid_a', 'result'),
                        DependencyDefinition('solid_b', 'result')
                    ]
                )
            }
        }

    In general, users should prefer not to construct this class directly or use the
    :py:class:`PipelineDefinition` API that requires instances of this class. Instead, use the
    :py:func:`@pipeline <pipeline>` API:

    .. code-block:: python

        @pipeline
        def pipeline():
            solid_c(solid_a(), solid_b())

    Args:
        solid (str): The name of the solid that is depended on, that is, from which the value
            passed between the two solids originates.
        output (Optional[str]): The name of the output that is depended on. (default: "result")
        description (Optional[str]): Human-readable description of this dependency.
    '''

    def __new__(cls, dependencies):
        deps = check.list_param(dependencies, 'dependencies', of_type=DependencyDefinition)
        seen = {}
        for dep in deps:
            key = dep.solid + ':' + dep.output
            if key in seen:
                raise DagsterInvalidDefinitionError(
                    'Duplicate dependencies on solid "{dep.solid}" output "{dep.output}" '
                    'used in the same MultiDependencyDefinition.'.format(dep=dep)
                )
            seen[key] = True

        return super(MultiDependencyDefinition, cls).__new__(cls, deps)

    def get_definitions(self):
        return self.dependencies

    def is_multi(self):
        return True
