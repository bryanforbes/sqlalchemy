# ext/mypy/plugin.py
# Copyright (C) 2021 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

"""
Mypy plugin for SQLAlchemy ORM.

"""
from typing import Callable
from typing import List
from typing import Optional
from typing import Tuple
from typing import Type as TypingType
from typing import Union

from mypy import nodes
from mypy.mro import calculate_mro
from mypy.mro import MroError
from mypy.nodes import Block
from mypy.nodes import ClassDef
from mypy.nodes import GDEF
from mypy.nodes import MypyFile
from mypy.nodes import NameExpr
from mypy.nodes import SymbolTable
from mypy.nodes import SymbolTableNode
from mypy.nodes import TypeInfo
from mypy.plugin import AttributeContext
from mypy.plugin import ClassDefContext
from mypy.plugin import DynamicClassDefContext
from mypy.plugin import Plugin
from mypy.plugin import SemanticAnalyzerPluginInterface
from mypy.types import get_proper_type
from mypy.types import Instance
from mypy.types import Type

from . import decl_class
from . import names
from . import util


class SQLAlchemyPlugin(Plugin):
    def get_dynamic_class_hook(
        self, fullname: str
    ) -> Optional[Callable[[DynamicClassDefContext], None]]:
        if names._type_id_for_fullname(fullname) is names.DECLARATIVE_BASE:
            return _dynamic_class_hook
        return None

    def get_customize_class_mro_hook(
        self, fullname: str
    ) -> Optional[Callable[[ClassDefContext], None]]:
        return _fill_in_decorators

    def get_class_decorator_hook(
        self, fullname: str
    ) -> Optional[Callable[[ClassDefContext], None]]:

        sym = self.lookup_fully_qualified(fullname)

        if sym is not None and sym.node is not None:
            type_id = names._type_id_for_named_node(sym.node)
            if type_id is names.MAPPED_DECORATOR:
                return _cls_decorator_hook
            elif type_id in (
                names.AS_DECLARATIVE,
                names.AS_DECLARATIVE_BASE,
            ):
                return _base_cls_decorator_hook
            elif type_id is names.DECLARATIVE_MIXIN:
                return _declarative_mixin_hook

        return None

    def get_metaclass_hook(
        self, fullname: str
    ) -> Optional[Callable[[ClassDefContext], None]]:
        if names._type_id_for_fullname(fullname) is names.DECLARATIVE_META:
            # Set any classes that explicitly have metaclass=DeclarativeMeta
            # as declarative so the check in `get_base_class_hook()` works
            return _metaclass_cls_hook

        return None

    def get_base_class_hook(
        self, fullname: str
    ) -> Optional[Callable[[ClassDefContext], None]]:
        sym = self.lookup_fully_qualified(fullname)

        if (
            sym
            and isinstance(sym.node, TypeInfo)
            and util._is_declarative(sym.node)
        ):
            return _base_cls_hook

        return None

    # def get_function_hook(
    #     self, fullname: str
    # ) -> Optional[Callable[[FunctionContext], Type]]:
    #     if fullname.endswith(".as_declarative"):

    #         def hook(ctx: FunctionContext) -> Type:
    #             import ipdb

    #             ipdb.set_trace()
    #             return ctx.default_return_type

    #         return hook

    #     if names._type_id_for_fullname(fullname) is names.COLUMN:

    #         def hook(ctx: FunctionContext) -> Type:
    #             import ipdb

    #             ipdb.set_trace()
    #             return ctx.default_return_type

    #         return hook

    #     return None

    def get_attribute_hook(
        self, fullname: str
    ) -> Optional[Callable[[AttributeContext], Type]]:
        if fullname.startswith(
            "sqlalchemy.orm.attributes.QueryableAttribute."
        ):
            return _queryable_getattr_hook

        return None

    def get_additional_deps(
        self, file: MypyFile
    ) -> List[Tuple[int, str, int]]:
        return [
            (10, "sqlalchemy.orm.attributes", -1),
            (10, "sqlalchemy.orm.decl_api", -1),
        ]


def _dynamic_class_hook(ctx: DynamicClassDefContext) -> None:
    """Generate a declarative Base class when the declarative_base() function
    is encountered."""

    _add_globals(ctx)

    cls = ClassDef(ctx.name, Block([]))
    cls.fullname = ctx.api.qualified_name(ctx.name)

    info = TypeInfo(SymbolTable(), cls, ctx.api.cur_mod_id)
    cls.info = info
    _set_declarative_metaclass(ctx.api, cls)

    cls_arg = util._get_callexpr_kwarg(ctx.call, "cls", expr_types=(NameExpr,))
    if cls_arg is not None and isinstance(cls_arg.node, TypeInfo):
        cls_metadata = util.SQLAlchemyMetadata(cls_arg.node, is_base=True)
        cls_arg.node.metadata["sqlalchemy"] = cls_metadata.serialize()
        decl_class._scan_declarative_assignments_and_apply_types(
            cls_arg.node.defn, ctx.api, is_mixin_scan=True
        )
        info.bases = [Instance(cls_arg.node, [])]
    else:
        obj = ctx.api.named_type("__builtins__.object")

        info.bases = [obj]

    try:
        calculate_mro(info)
    except MroError:
        util.fail(
            ctx.api, "Not able to calculate MRO for declarative base", ctx.call
        )
        obj = ctx.api.named_type("__builtins__.object")
        info.bases = [obj]
        info.fallback_to_any = True

    info.metadata["sqlalchemy"] = util.SQLAlchemyMetadata(
        info, is_base=True
    ).serialize()
    ctx.api.add_symbol_table_node(ctx.name, SymbolTableNode(GDEF, info))


def _fill_in_decorators(ctx: ClassDefContext) -> None:
    for decorator in ctx.cls.decorators:
        # set the ".fullname" attribute of a class decorator
        # that is a MemberExpr.   This causes the logic in
        # semanal.py->apply_class_plugin_hooks to invoke the
        # get_class_decorator_hook for our "registry.map_class()"
        # and "registry.as_declarative_base()" methods.
        # this seems like a bug in mypy that these decorators are otherwise
        # skipped.

        if (
            isinstance(decorator, nodes.CallExpr)
            and isinstance(decorator.callee, nodes.MemberExpr)
            and decorator.callee.name == "as_declarative_base"
        ):
            target = decorator.callee
        elif (
            isinstance(decorator, nodes.MemberExpr)
            and decorator.name == "mapped"
        ):
            target = decorator
        else:
            continue

        assert isinstance(target.expr, NameExpr)
        sym = ctx.api.lookup_qualified(
            target.expr.name, target, suppress_errors=True
        )
        if sym and sym.node:
            sym_type = get_proper_type(sym.type)
            if isinstance(sym_type, Instance):
                target.fullname = f"{sym_type.type.fullname}.{target.name}"
            else:
                # if the registry is in the same file as where the
                # decorator is used, it might not have semantic
                # symbols applied and we can't get a fully qualified
                # name or an inferred type, so we are actually going to
                # flag an error in this case that they need to annotate
                # it.  The "registry" is declared just
                # once (or few times), so they have to just not use
                # type inference for its assignment in this one case.
                util.fail(
                    ctx.api,
                    "Class decorator called %s(), but we can't "
                    "tell if it's from an ORM registry.  Please "
                    "annotate the registry assignment, e.g. "
                    "my_registry: registry = registry()" % target.name,
                    sym.node,
                )


def _cls_decorator_hook(ctx: ClassDefContext) -> None:
    _add_globals(ctx)
    assert isinstance(ctx.reason, nodes.MemberExpr)
    expr = ctx.reason.expr

    assert isinstance(expr, nodes.RefExpr) and isinstance(expr.node, nodes.Var)

    node_type = get_proper_type(expr.node.type)

    assert (
        isinstance(node_type, Instance)
        and names._type_id_for_named_node(node_type.type) is names.REGISTRY
    )

    util._set_declarative_base(ctx.cls.info)
    decl_class._scan_declarative_assignments_and_apply_types(ctx.cls, ctx.api)


def _base_cls_decorator_hook(ctx: ClassDefContext) -> None:
    _add_globals(ctx)

    cls = ctx.cls
    _set_declarative_metaclass(ctx.api, cls)

    util._set_declarative_base(ctx.cls.info)
    decl_class._scan_declarative_assignments_and_apply_types(
        cls, ctx.api, is_mixin_scan=True
    )


def _declarative_mixin_hook(ctx: ClassDefContext) -> None:
    _add_globals(ctx)
    util._set_declarative_base(ctx.cls.info)
    decl_class._scan_declarative_assignments_and_apply_types(
        ctx.cls, ctx.api, is_mixin_scan=True
    )


def _metaclass_cls_hook(ctx: ClassDefContext) -> None:
    util._set_declarative_base(ctx.cls.info)


def _base_cls_hook(ctx: ClassDefContext) -> None:
    _add_globals(ctx)
    decl_class._scan_declarative_assignments_and_apply_types(ctx.cls, ctx.api)


def _queryable_getattr_hook(ctx: AttributeContext) -> Type:
    # how do I....tell it it has no attribute of a certain name?
    # can't find any Type that seems to match that
    return ctx.default_attr_type


def plugin(version: str) -> TypingType[SQLAlchemyPlugin]:
    return SQLAlchemyPlugin


def _add_globals(ctx: Union[ClassDefContext, DynamicClassDefContext]) -> None:
    """Add __sa_DeclarativeMeta and __sa_Mapped symbol to the global space
    for all class defs

    """

    util.add_global(ctx, "sqlalchemy.orm.attributes", "Mapped", "__sa_Mapped")


def _set_declarative_metaclass(
    api: SemanticAnalyzerPluginInterface, target_cls: ClassDef
) -> None:
    info = target_cls.info
    sym = api.lookup_fully_qualified_or_none(
        "sqlalchemy.orm.decl_api.DeclarativeMeta"
    )
    assert sym is not None and isinstance(sym.node, TypeInfo)
    info.declared_metaclass = info.metaclass_type = Instance(sym.node, [])
