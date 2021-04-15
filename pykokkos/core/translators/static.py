import ast
import copy
import sys
from typing import Dict, List, Optional, Tuple

from pykokkos.core import cppast
from pykokkos.core.visitors import (
    ClasstypeVisitor, KokkosFunctionVisitor, KokkosMainVisitor, WorkunitVisitor
)
from pykokkos.core.parsers import PyKokkosEntity, PyKokkosStyles
from pykokkos.interface import DataType

from .bindings import bind_main, bind_workunits
from .functor import generate_functor
from .members import PyKokkosMembers
from .symbols_pass import SymbolsPass

class StaticTranslator:
    """
    Translates a PyKokkos workload to C++ using static analysis only
    """

    def __init__(self, module: str, functor: str):
        """
        StaticTranslator Constructor

        :param module: the name of the compiled Python module
        :param functor: the name of the generated functor file
        """

        self.pk_import: str
        self.pk_members: PyKokkosMembers

        self.module_file: str = module
        self.functor_file: str = functor

    def translate(
        self,
        entity: PyKokkosEntity,
        classtypes: List[PyKokkosEntity],
    ) -> Tuple[List[str], List[str]]:
        """
        Translate an entity into C++ code

        :param entity: the type of the entity being translated
        :param classtypes: the list of classtypes needed by the entity
        :returns: a tuple of lists of strings of representing the functor code and bindings respectively
        """

        self.pk_import = entity.pk_import

        entity.AST = self.add_parent_refs(entity.AST)
        for c in classtypes:
            c.AST = self.add_parent_refs(c.AST)

        self.pk_members = PyKokkosMembers()
        self.pk_members.extract(entity, classtypes)
        self.check_symbols(classtypes, entity.path)

        source: Tuple[List[str], int] = entity.source
        functor_name: str = f"pk_functor_{entity.name.declname}"

        classtypes: List[cppast.RecordDecl] = self.translate_classtypes(classtypes)
        functions: List[cppast.MethodDecl] = self.translate_functions(source)

        workunits: Dict[cppast.DeclRefExpr, Tuple[str, cppast.MethodDecl]]
        workunits = self.translate_workunits(source)

        struct: cppast.RecordDecl = generate_functor(functor_name, self.pk_members, workunits, functions)

        bindings: List[str] = self.generate_bindings(entity, functor_name, source, workunits)

        s = cppast.Serializer()
        functor: List[str] = [self.generate_header()]
        functor.extend([s.serialize(c) for c in classtypes])
        functor.append(s.serialize(struct))

        bindings.insert(0, self.generate_includes())
        bindings.insert(0, self.generate_header())

        return functor, bindings

    def add_parent_refs(self, classdef: ast.ClassDef) -> ast.ClassDef:
        """
        Add references to each node's parent node in classdef

        :param classdef: the classdef being modified
        :returns: the modified classdef
        """

        for node in ast.walk(classdef):
            for child in ast.iter_child_nodes(node):
                child.parent = node

        return classdef

    def check_symbols(self, classtypes: List[PyKokkosEntity], path: str) -> None:
        """
        Pass over PyKokkos code and make sure that all symbols are
        valid, printing error messages and exiting if any errors are
        found

        :param classtypes: the list of PyKokkos classtypes
        :param path: the path to the file being translated
        """

        symbols_pass = SymbolsPass(self.pk_members, self.pk_import, path)

        error_messages: List[str] = []
        for AST in self.pk_members.pk_mains.values():
            error_messages.extend(symbols_pass.check_symbols(AST))
        for AST in self.pk_members.pk_workunits.values():
            error_messages.extend(symbols_pass.check_symbols(AST))
        for AST in self.pk_members.pk_functions.values():
            error_messages.extend(symbols_pass.check_symbols(AST))
        for entity in classtypes:
            error_messages.extend(symbols_pass.check_symbols(entity.AST))

        if error_messages:
            for error in error_messages:
                print(error)

            sys.exit()


    def translate_classtypes(self, classtypes: List[PyKokkosEntity]) -> List[cppast.RecordDecl]:
        """
        Translate all classtypes, i.e. classes that the workload uses internally

        :param classtypes: the list of classtypes needed by the workload
        :returns: a list of strings of translated source code
        """

        declarations: List[cppast.RecordDecl] = []
        definitions: List[cppast.RecordDecl] = []

        for c in classtypes:
            classdef: ast.ClassDef = c.AST
            source: Tuple[List[str], int] = c.source

            node_visitor = ClasstypeVisitor(
                {},
                source, self.pk_members.views, self.pk_members.pk_workunits,
                self.pk_members.fields, self.pk_members.pk_functions,
                self.pk_members.classtype_methods, self.pk_import, debug=True
            )

            definition: cppast.RecordDecl = node_visitor.visit(classdef)
            declaration = copy.deepcopy(definition)
            declaration.is_definition = False

            definitions.append(definition)
            declarations.append(declaration)

        return declarations + definitions

    def translate_functions(self, source: Tuple[List[str], int]) -> List[cppast.MethodDecl]:
        """
        Translate all PyKokkos functions

        :param source: the python source code of the workload
        :returns: a list of method declarations
        """

        node_visitor = KokkosFunctionVisitor(
            {},
            source, self.pk_members.views, self.pk_members.pk_workunits,
            self.pk_members.fields, self.pk_members.pk_functions,
            self.pk_members.classtype_methods, self.pk_import, debug=True)

        translation: List[cppast.MethodDecl] = []

        for functiondef in self.pk_members.pk_functions.values():
            translation.append(node_visitor.visit(functiondef))

        return translation

    def translate_workunits(self, source: Tuple[List[str], int]) -> Dict[cppast.DeclRefExpr, Tuple[str, cppast.MethodDecl]]:
        """
        Translate the workunits

        :param source: the python source code of the workload
        :returns: a dictionary mapping from workload name to a tuple of operation name and source
        """

        node_visitor = WorkunitVisitor(
            {}, source, self.pk_members.views, self.pk_members.pk_workunits,
            self.pk_members.fields, self.pk_members.pk_functions,
            self.pk_members.classtype_methods, self.pk_import, debug=True)

        workunits: Dict[cppast.DeclRefExpr, Tuple[str, cppast.MethodDecl]] = {}

        for n, w in self.pk_members.pk_workunits.items():
            try:
                workunits[n] = node_visitor.visit(w)
            except:
                print(f"Translation of {w} failed")
                sys.exit(1)

        return workunits

    def generate_header(self) -> str:
        """
        Generate the commented header at the top of the C++ source file

        :returns: the header as a string
        """

        return "// ******* AUTOMATICALLY GENERATED BY PyKokkos *******"

    def generate_includes(self) -> str:
        """
        Generate the list of include statements

        :returns: the includes as a string
        """

        headers: List[str] = [
            "pybind11/pybind11.h",
            "Kokkos_Core.hpp",
            "Kokkos_Sort.hpp",
            "fstream",
            "iostream",
            "cmath",
            self.functor_file
        ]
        headers = [f"#include <{h}>\n" for h in headers]

        return "".join(headers)

    def generate_bindings(
        self,
        entity: PyKokkosEntity,
        functor_name: str,
        source: Tuple[List[str], int],
        workunits: Dict[cppast.DeclRefExpr, Tuple[str, cppast.MethodDecl]]
    ) -> List[str]:
        """
        Generate the pybind bindings for a single real precision

        :param entity: the type of the entity being translated
        :param functor_name: the name of the functor
        :param workunits: the translated workunits
        :returns: the source as a list of strings
        """

        bindings: List[str]
        if entity.style is PyKokkosStyles.workload:
            bindings = bind_main(functor_name, self.pk_members, source, self.pk_import, self.module_file)
        else:
            bindings = bind_workunits(functor_name, self.pk_members, workunits, self.module_file)

        return bindings