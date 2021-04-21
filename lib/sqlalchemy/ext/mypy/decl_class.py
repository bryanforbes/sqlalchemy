# ext/mypy/decl_class.py
# Copyright (C) 2021 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

from typing import Optional

from mypy import nodes
from mypy.nodes import AssignmentStmt
from mypy.nodes import CallExpr
from mypy.nodes import ClassDef
from mypy.nodes import Decorator
from mypy.nodes import ListExpr
from mypy.nodes import MemberExpr
from mypy.nodes import NameExpr
from mypy.nodes import PlaceholderNode
from mypy.nodes import RefExpr
from mypy.nodes import StrExpr
from mypy.nodes import SymbolNode
from mypy.nodes import TempNode
from mypy.nodes import TypeInfo
from mypy.nodes import Var
from mypy.plugin import SemanticAnalyzerPluginInterface
from mypy.types import AnyType
from mypy.types import CallableType
from mypy.types import get_proper_type
from mypy.types import Instance
from mypy.types import NoneType
from mypy.types import ProperType
from mypy.types import Type
from mypy.types import TypeOfAny
from mypy.types import UnboundType
from mypy.types import UnionType

from . import apply
from . import infer
from . import names
from . import util


class NotReady(Exception):
    pass


def _get_metadata_for_class(
    api: SemanticAnalyzerPluginInterface,
    cls: ClassDef,
    *,
    is_base: bool = True
) -> Optional[util.SQLAlchemyMetadata]:
    metadata = util.SQLAlchemyMetadata(cls.info, is_base=is_base)

    try:
        for stmt in util._flatten_typechecking(cls.defs.body):
            if isinstance(stmt, AssignmentStmt):
                _parse_assignment_for_metadata(api, cls, stmt, metadata)
            elif isinstance(stmt, Decorator):
                pass
    except NotReady:
        return None
    else:
        return metadata


def _parse_assignment_for_metadata(
    api: SemanticAnalyzerPluginInterface,
    cls: ClassDef,
    stmt: AssignmentStmt,
    metadata: util.SQLAlchemyMetadata,
) -> None:
    lvalue = stmt.lvalues[0]
    if not isinstance(lvalue, NameExpr):
        return

    sym = cls.info.names.get(lvalue.name)
    if sym is None:
        # This name is likely blocked by a star import.
        # We don't need to defer because defer() is
        # already called by mark_incomplete().
        return

    node = sym.node

    if isinstance(node, PlaceholderNode):
        # This node is not ready yet.
        raise NotReady()

    assert isinstance(node, Var)

    # x: ClassVar[int] is ignored.
    if node.is_classvar:
        return

    if node.name == "__abstract__":
        if api.parse_bool(stmt.rvalue) is True:
            metadata.is_base = True
        return
    if node.name == "__tablename__":
        metadata.has_table = True
        return
    if node.name.startswith("__"):
        return
    if node.name == "_mypy_mapped_attrs":
        if not isinstance(stmt.rvalue, ListExpr):
            util.fail(
                api,
                "_mypy_mapped_attrs is expected to be a list",
                stmt,
            )
        else:
            for item in stmt.rvalue.items:
                if isinstance(item, (NameExpr, StrExpr)):
                    apply._apply_mypy_mapped_attr(cls, api, item, metadata)
        return

    left_hand_mapped_type: Optional[Type] = None
    left_hand_explicit_type: Optional[ProperType] = None

    if not node.is_inferred:
        node_type = get_proper_type(node.type)
        if (
            isinstance(node_type, Instance)
            and names._type_id_for_named_node(node_type.type) is names.MAPPED
        ):
            left_hand_explicit_type = get_proper_type(node_type.args[0])
            left_hand_mapped_type = node_type
        else:
            left_hand_explicit_type = node_type
            left_hand_mapped_type = None

    if isinstance(stmt.rvalue, TempNode) and left_hand_mapped_type is not None:
        python_type_for_type = left_hand_explicit_type
    elif isinstance(stmt.rvalue, CallExpr) and isinstance(
        stmt.rvalue.callee, NameExpr
    ):
        python_type_for_type = infer._infer_type_from_right_hand_nameexpr(
            api, stmt, node, left_hand_explicit_type, stmt.rvalue.callee
        )

    if python_type_for_type is None:
        return

    metadata.attributes.append(
        util.SQLAlchemyAttribute(
            node.name, stmt.line, stmt.column, python_type_for_type, cls.info
        )
    )


def _scan_declarative_assignments_and_apply_types(
    cls: ClassDef,
    api: SemanticAnalyzerPluginInterface,
    is_mixin_scan: bool = False,
) -> None:
    if cls.fullname.startswith("builtins."):
        return

    if "_sa_decl_class_applied" in cls.info.metadata:
        cls_metadata = util.DeclClassApplied.deserialize(
            cls.info.metadata["_sa_decl_class_applied"], api
        )

        # ensure that a class that's mapped is always picked up by
        # its mapped() decorator or declarative metaclass before
        # it would be detected as an unmapped mixin class
        if not is_mixin_scan:
            assert cls_metadata.is_mapped

            # mypy can call us more than once.  it then will have reset the
            # left hand side of everything, but not the right that we removed,
            # removing our ability to re-scan.   but we have the types
            # here, so lets re-apply them.

            apply._re_apply_declarative_assignments(cls, api, cls_metadata)

        return

    cls_metadata = util.DeclClassApplied(not is_mixin_scan, False, [], [])

    for stmt in util._flatten_typechecking(cls.defs.body):
        if isinstance(stmt, AssignmentStmt):
            _scan_declarative_assignment_stmt(cls, api, stmt, cls_metadata)
        elif isinstance(stmt, Decorator):
            _scan_declarative_decorator_stmt(cls, api, stmt, cls_metadata)
    _scan_for_mapped_bases(cls, api, cls_metadata)

    if not is_mixin_scan:
        apply._add_additional_orm_attributes(cls, api, cls_metadata)

    cls.info.metadata["_sa_decl_class_applied"] = cls_metadata.serialize()


def _scan_declarative_decorator_stmt(
    cls: ClassDef,
    api: SemanticAnalyzerPluginInterface,
    stmt: Decorator,
    cls_metadata: util.DeclClassApplied,
) -> None:
    """Extract mapping information from a @declared_attr in a declarative
    class.

    E.g.::

        @reg.mapped
        class MyClass:
            # ...

            @declared_attr
            def updated_at(cls) -> Column[DateTime]:
                return Column(DateTime)

    Will resolve in mypy as::

        @reg.mapped
        class MyClass:
            # ...

            updated_at: Mapped[Optional[datetime.datetime]]

    """
    for dec in stmt.decorators:
        if (
            isinstance(dec, (NameExpr, MemberExpr, SymbolNode))
            and names._type_id_for_named_node(dec) is names.DECLARED_ATTR
        ):
            break
    else:
        return

    dec_index = cls.defs.body.index(stmt)

    left_hand_explicit_type: Optional[ProperType] = None

    if isinstance(stmt.func.type, CallableType):
        func_type = stmt.func.type.ret_type
        if isinstance(func_type, UnboundType):
            type_id = names._type_id_for_unbound_type(func_type, cls, api)
        else:
            # this does not seem to occur unless the type argument is
            # incorrect
            return

        if (
            type_id
            in {
                names.MAPPED,
                names.RELATIONSHIP,
                names.COMPOSITE_PROPERTY,
                names.MAPPER_PROPERTY,
                names.SYNONYM_PROPERTY,
                names.COLUMN_PROPERTY,
            }
            and func_type.args
        ):
            left_hand_explicit_type = get_proper_type(func_type.args[0])
        elif type_id is names.COLUMN and func_type.args:
            typeengine_arg = func_type.args[0]
            if isinstance(typeengine_arg, UnboundType):
                sym = api.lookup_qualified(typeengine_arg.name, typeengine_arg)
                if sym is not None and isinstance(sym.node, TypeInfo):
                    if names._has_base_type_id(sym.node, names.TYPEENGINE):
                        left_hand_explicit_type = UnionType(
                            [
                                infer._extract_python_type_from_typeengine(
                                    api, sym.node, []
                                ),
                                NoneType(),
                            ]
                        )
                    else:
                        util.fail(
                            api,
                            "Column type should be a TypeEngine "
                            "subclass not '{}'".format(sym.node.fullname),
                            func_type,
                        )

    if left_hand_explicit_type is None:
        # no type on the decorated function.  our option here is to
        # dig into the function body and get the return type, but they
        # should just have an annotation.
        msg = (
            "Can't infer type from @declared_attr on function '{}';  "
            "please specify a return type from this function that is "
            "one of: Mapped[<python type>], relationship[<target class>], "
            "Column[<TypeEngine>], MapperProperty[<python type>]"
        )
        util.fail(api, msg.format(stmt.var.name), stmt)

        left_hand_explicit_type = AnyType(TypeOfAny.special_form)

    left_node = NameExpr(stmt.var.name)
    left_node.node = stmt.var

    # totally feeling around in the dark here as I don't totally understand
    # the significance of UnboundType.  It seems to be something that is
    # not going to do what's expected when it is applied as the type of
    # an AssignmentStatement.  So do a feeling-around-in-the-dark version
    # of converting it to the regular Instance/TypeInfo/UnionType structures
    # we see everywhere else.
    if isinstance(left_hand_explicit_type, UnboundType):
        left_hand_explicit_type = get_proper_type(
            util._unbound_to_instance(api, left_hand_explicit_type)
        )

    left_node.node.type = util._mapped_instance(api, [left_hand_explicit_type])

    # this will ignore the rvalue entirely
    # rvalue = TempNode(AnyType(TypeOfAny.special_form))

    # rewrite the node as:
    # <attr> : Mapped[<typ>] =
    # _sa_Mapped._empty_constructor(lambda: <function body>)
    # the function body is maintained so it gets type checked internally
    column_descriptor = nodes.NameExpr("__sa_Mapped")
    column_descriptor.fullname = "sqlalchemy.orm.attributes.Mapped"
    mm = nodes.MemberExpr(column_descriptor, "_empty_constructor")

    arg = nodes.LambdaExpr(stmt.func.arguments, stmt.func.body)
    rvalue = CallExpr(
        mm,
        [arg],
        [nodes.ARG_POS],
        ["arg1"],
    )

    new_stmt = AssignmentStmt([left_node], rvalue)
    new_stmt.type = left_node.node.type

    cls_metadata.mapped_attr_names.append(
        (left_node.name, left_hand_explicit_type)
    )
    cls.defs.body[dec_index] = new_stmt


def _scan_declarative_assignment_stmt(
    cls: ClassDef,
    api: SemanticAnalyzerPluginInterface,
    stmt: AssignmentStmt,
    cls_metadata: util.DeclClassApplied,
) -> None:
    """Extract mapping information from an assignment statement in a
    declarative class.

    """
    lvalue = stmt.lvalues[0]
    if not isinstance(lvalue, NameExpr):
        return

    sym = cls.info.names.get(lvalue.name)

    # this establishes that semantic analysis has taken place, which
    # means the nodes are populated and we are called from an appropriate
    # hook.
    assert sym is not None
    node = sym.node

    if isinstance(node, PlaceholderNode):
        return

    assert node is lvalue.node
    assert isinstance(node, Var)

    if node.name == "__abstract__":
        if api.parse_bool(stmt.rvalue) is True:
            cls_metadata.is_mapped = False
        return
    elif node.name == "__tablename__":
        cls_metadata.has_table = True
    elif node.name.startswith("__"):
        return
    elif node.name == "_mypy_mapped_attrs":
        if not isinstance(stmt.rvalue, ListExpr):
            util.fail(api, "_mypy_mapped_attrs is expected to be a list", stmt)
        else:
            for item in stmt.rvalue.items:
                if isinstance(item, (NameExpr, StrExpr)):
                    apply._apply_mypy_mapped_attr(cls, api, item, cls_metadata)

    left_hand_mapped_type: Optional[Type] = None
    left_hand_explicit_type: Optional[ProperType] = None

    if node.is_inferred or node.type is None:
        if isinstance(stmt.type, UnboundType):
            # look for an explicit Mapped[] type annotation on the left
            # side with nothing on the right

            # print(stmt.type)
            # Mapped?[Optional?[A?]]

            left_hand_explicit_type = stmt.type

            if stmt.type.name == "Mapped":
                mapped_sym = api.lookup_qualified("Mapped", cls)
                if (
                    mapped_sym is not None
                    and mapped_sym.node is not None
                    and names._type_id_for_named_node(mapped_sym.node)
                    is names.MAPPED
                ):
                    left_hand_explicit_type = get_proper_type(
                        stmt.type.args[0]
                    )
                    left_hand_mapped_type = stmt.type

            # TODO: do we need to convert from unbound for this case?
            # left_hand_explicit_type = util._unbound_to_instance(
            #     api, left_hand_explicit_type
            # )
    else:
        node_type = get_proper_type(node.type)
        if (
            isinstance(node_type, Instance)
            and names._type_id_for_named_node(node_type.type) is names.MAPPED
        ):
            # print(node.type)
            # sqlalchemy.orm.attributes.Mapped[<python type>]
            left_hand_explicit_type = get_proper_type(node_type.args[0])
            left_hand_mapped_type = node_type
        else:
            # print(node.type)
            # <python type>
            left_hand_explicit_type = node_type
            left_hand_mapped_type = None

    if isinstance(stmt.rvalue, TempNode) and left_hand_mapped_type is not None:
        # annotation without assignment and Mapped is present
        # as type annotation
        # equivalent to using _infer_type_from_left_hand_type_only.

        python_type_for_type = left_hand_explicit_type
    elif isinstance(stmt.rvalue, CallExpr) and isinstance(
        stmt.rvalue.callee, RefExpr
    ):

        type_id = names._type_id_for_callee(stmt.rvalue.callee)

        if type_id is None:
            return
        elif type_id is names.COLUMN:
            python_type_for_type = infer._infer_type_from_decl_column(
                api, stmt, node, left_hand_explicit_type, stmt.rvalue
            )
        elif type_id is names.RELATIONSHIP:
            python_type_for_type = infer._infer_type_from_relationship(
                api, stmt, node, left_hand_explicit_type
            )
        elif type_id is names.COLUMN_PROPERTY:
            python_type_for_type = infer._infer_type_from_decl_column_property(
                api, stmt, node, left_hand_explicit_type
            )
        elif type_id is names.SYNONYM_PROPERTY:
            python_type_for_type = infer._infer_type_from_left_hand_type_only(
                api, node, left_hand_explicit_type
            )
        elif type_id is names.COMPOSITE_PROPERTY:
            python_type_for_type = (
                infer._infer_type_from_decl_composite_property(
                    api, stmt, node, left_hand_explicit_type
                )
            )
        else:
            return

    else:
        return

    assert python_type_for_type is not None

    cls_metadata.mapped_attr_names.append((node.name, python_type_for_type))

    apply._apply_type_to_mapped_statement(
        api,
        stmt,
        lvalue,
        left_hand_explicit_type,
        python_type_for_type,
    )


def _scan_for_mapped_bases(
    cls: ClassDef,
    api: SemanticAnalyzerPluginInterface,
    cls_metadata: util.DeclClassApplied,
) -> None:
    """Given a class, iterate through its superclass hierarchy to find
    all other classes that are considered as ORM-significant.

    Locates non-mapped mixins and scans them for mapped attributes to be
    applied to subclasses.

    """

    baseclasses = list(cls.info.bases)

    while baseclasses:
        base: Instance = baseclasses.pop(0)

        if base.type.fullname.startswith("builtins."):
            continue

        if "_sa_decl_class_applied" in base.type.metadata:
            cls_metadata.mapped_mro.append(base)

        baseclasses.extend(base.type.bases)
