"""Snapshot-local static call resolution for the CodeKG Python IR.

The resolver intentionally has no Neo4j dependency.  It operates solely on
the ``RepositoryIR`` currently being loaded and list-valued symbol indexes, so
that duplicate qualified names remain visible as ambiguity rather than being
silently overwritten.  A resolution is a fact about one syntactic call site;
the loader turns successful resolutions into graph relationships.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from codekg.ir import CallIR, FileIR, ImportIR, LocalBindingIR, RepositoryIR

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_DOTTED_IDENTIFIER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")

EXACT_RESOLUTION_STATUSES = frozenset(
    {
        "exact_local",
        "exact_import",
        "self_direct",
        "cls_direct",
        "inherited_method",
        "super_method",
        "local_receiver",
        "local_receiver_inherited",
    }
)
CONSTRUCTOR_RESOLUTION_STATUSES = frozenset(
    {
        "constructor_exact_local",
        "constructor_exact_import",
        "constructor_ambiguous",
    }
)


@dataclass(frozen=True)
class SymbolRef:
    """A graph identity known to the resolver, scoped to one snapshot."""

    key: str
    qname: str
    path: str
    kind: str
    parent_qname: str | None = None
    return_annotation: str | None = None


@dataclass(frozen=True)
class BaseState:
    """The resolved direct bases for one type, including incomplete state."""

    bases: tuple[str, ...] = ()
    incomplete: bool = False


@dataclass(frozen=True)
class CallResolution:
    """The resolver's complete, lossless conclusion for a call site."""

    call: CallIR
    path: str
    owner_key: str | None
    status: str
    candidate_keys: tuple[str, ...]
    target_key: str | None = None
    construction_target_key: str | None = None
    initializer_candidate_keys: tuple[str, ...] = ()
    initializer_target_key: str | None = None
    initializer_status: str | None = None

    @property
    def is_exact(self) -> bool:
        return self.status in EXACT_RESOLUTION_STATUSES and self.target_key is not None

    @property
    def is_constructor(self) -> bool:
        return self.status in CONSTRUCTOR_RESOLUTION_STATUSES

    @property
    def is_construction_exact(self) -> bool:
        return self.status in {"constructor_exact_local", "constructor_exact_import"}


class ResolverIndex:
    """Lookup boundary used by the resolution algorithm.

    The in-memory implementation keeps the transactional path unchanged.  The
    sharded exporter supplies the same operations from its SQLite registry;
    importantly, candidate lists remain list-valued and key ordered.
    """

    def owners(self, path: str, qname: str) -> tuple[SymbolRef, ...]:
        raise NotImplementedError

    def callables(self, qname: str) -> tuple[SymbolRef, ...]:
        raise NotImplementedError

    def types(self, qname: str) -> tuple[SymbolRef, ...]:
        raise NotImplementedError

    def file(self, path: str) -> FileIR | None:
        raise NotImplementedError

    def files(self) -> Iterable[FileIR]:
        raise NotImplementedError

    def base_state(self, type_qname: str) -> BaseState:
        raise NotImplementedError

    def module_owner(self, language: str, module_qname: str) -> str | None:
        raise NotImplementedError


class InMemoryResolverIndex(ResolverIndex):
    """Legacy resolver data with the lookup contract used by bulk export."""

    def __init__(
        self,
        repo: RepositoryIR,
        *,
        owners_by_file_qname: Mapping[tuple[str, str], Iterable[SymbolRef]],
        callables: Iterable[SymbolRef],
        types: Iterable[SymbolRef],
    ) -> None:
        self._owners = {
            key: tuple(sorted(values, key=lambda value: value.key))
            for key, values in owners_by_file_qname.items()
        }
        self._callables = _group_by_qname(callables)
        self._types = _group_by_qname(types)
        self._files = {file.path: file for file in repo.files}
        self._base_states = _base_states_from_files(repo.files, self)
        self._module_owners: dict[tuple[str, str], str] = {}
        for file in sorted(repo.files, key=lambda item: item.path):
            self._module_owners.setdefault((file.language, file.module_qname), file.path)

    def owners(self, path: str, qname: str) -> tuple[SymbolRef, ...]:
        return self._owners.get((path, qname), ())

    def callables(self, qname: str) -> tuple[SymbolRef, ...]:
        return self._callables.get(qname, ())

    def types(self, qname: str) -> tuple[SymbolRef, ...]:
        return self._types.get(qname, ())

    def file(self, path: str) -> FileIR | None:
        return self._files.get(path)

    def files(self) -> Iterable[FileIR]:
        return self._files.values()

    def base_state(self, type_qname: str) -> BaseState:
        return self._base_states.get(type_qname, BaseState())

    def module_owner(self, language: str, module_qname: str) -> str | None:
        return self._module_owners.get((language, module_qname))


class SqliteResolverIndex(ResolverIndex):
    """Read-only lookup backend for the sharded export resolver registry."""

    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        self.connection.execute("PRAGMA cache_size=-32768")

    def close(self) -> None:
        self.connection.close()

    def module_owner(self, language: str, module_qname: str) -> str | None:
        row = self.connection.execute(
            "SELECT owner_path FROM module_owners WHERE language=? AND module_qname=?",
            (language, module_qname),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def owners(self, path: str, qname: str) -> tuple[SymbolRef, ...]:
        # Module initializers are not in symbols, so callers add their local
        # ModuleInit owner directly before asking this backend.
        return self._refs(
            "SELECT key, qname, path, kind, parent_qname, return_annotation "
            "FROM symbols WHERE path = ? AND qname = ? ORDER BY key",
            (path, qname),
        )

    def callables(self, qname: str) -> tuple[SymbolRef, ...]:
        return self._refs(
            "SELECT key, qname, path, kind, parent_qname, return_annotation "
            "FROM symbols WHERE qname = ? AND kind IN ('function', 'method') ORDER BY key",
            (qname,),
        )

    def types(self, qname: str) -> tuple[SymbolRef, ...]:
        return self._refs(
            "SELECT key, qname, path, kind, parent_qname, return_annotation "
            "FROM symbols WHERE qname = ? AND kind = 'type' ORDER BY key",
            (qname,),
        )

    def file(self, path: str) -> FileIR | None:
        row = self.connection.execute(
            "SELECT path, language, loc, module_qname, parse_status FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        imports = tuple(
            ImportIR(module, name, alias)
            for module, name, alias in self.connection.execute(
                "SELECT module, name, alias FROM imports WHERE path = ? ORDER BY ordinal",
                (path,),
            )
        )
        return FileIR(
            path=row[0],
            language=row[1],
            loc=row[2],
            module_qname=row[3],
            parse_status=row[4],
            imports=imports,
        )

    def files(self) -> Iterable[FileIR]:
        from codekg.bulk_spool import _file_from_normalized

        for (path,) in self.connection.execute("SELECT path FROM files ORDER BY ordinal"):
            yield _file_from_normalized(self.connection, str(path))

    def base_state(self, type_qname: str) -> BaseState:
        """Resolve only ``type_qname``'s bases without enumerating the registry.

        The query retains the legacy file/ordinal order and reads only the
        compact file/import context needed to expand import aliases.
        """
        bases: list[str] = []
        if len(self.types(type_qname)) != 1:
            return BaseState(incomplete=True)
        rows = self.connection.execute(
            "SELECT inheritance.path, inheritance.base_name, inheritance.base_qname "
            "FROM inheritance JOIN files ON files.path = inheritance.path "
            "WHERE inheritance.type_qname = ? "
            "ORDER BY files.ordinal, inheritance.ordinal",
            (type_qname,),
        )
        for path, base_name, base_qname in rows:
            file = self.file(str(path))
            assert file is not None
            candidates = _inheritance_candidates(
                str(base_name),
                str(base_qname) if base_qname is not None else None,
                _import_bindings(file),
            )
            parent_refs = {ref.key: ref for qname in candidates for ref in self.types(qname)}
            if len(parent_refs) != 1:
                return BaseState(incomplete=True)
            bases.append(next(iter(parent_refs.values())).qname)
        return BaseState(tuple(bases))

    def external_module_owner(self, module: str) -> str | None:
        """Return the deterministic file owner for a shared external module."""
        row = self.connection.execute(
            "SELECT path FROM imports WHERE module = ? ORDER BY path LIMIT 1", (module,)
        ).fetchone()
        return str(row[0]) if row else None

    def _refs(self, query: str, values: tuple[str, ...]) -> tuple[SymbolRef, ...]:
        rows = self.connection.execute(query, values)
        return tuple(SymbolRef(*row[:4], row[4], row[5] or None) for row in rows)


def resolve_call_sites(
    repo: RepositoryIR,
    *,
    owners_by_file_qname: Mapping[tuple[str, str], Iterable[SymbolRef]],
    callables: Iterable[SymbolRef],
    types: Iterable[SymbolRef],
) -> tuple[CallResolution, ...]:
    """Resolve every syntactic call site without consulting another snapshot.

    The supplied references must be constructed from ``repo`` only.  Keeping
    that boundary explicit makes cross-repository and cross-commit resolution
    impossible by construction.
    """

    resolver = _Resolver(
        InMemoryResolverIndex(
            repo,
            owners_by_file_qname=owners_by_file_qname,
            callables=callables,
            types=types,
        ),
    )
    return tuple(resolver.resolve(file, call) for file in repo.files for call in file.calls)


class _Resolver:
    def __init__(
        self,
        index: ResolverIndex,
    ) -> None:
        self.index = index
        self._mro_cache: OrderedDict[str, tuple[str, ...] | None] = OrderedDict()
        self._mro_cache_limit = 4096
        self._mro_cache_max_length = 256

    def resolve(self, file: FileIR, call: CallIR) -> CallResolution:
        owners = self.index.owners(file.path, call.owner_qname)
        if not owners:
            return CallResolution(call, file.path, None, "owner_unresolved", ())
        if len(owners) != 1:
            return CallResolution(
                call, file.path, None, "owner_ambiguous", tuple(owner.key for owner in owners)
            )

        owner = owners[0]
        if call.receiver_kind in {"self", "cls"}:
            if not _is_direct_receiver_call(call, call.receiver_kind):
                return CallResolution(call, file.path, owner.key, "dynamic", ())
            return self._resolve_receiver_method(file, call, owner, is_super=False)
        if call.receiver_kind == "super":
            return self._resolve_receiver_method(file, call, owner, is_super=True)
        if call.receiver_kind == "attribute":
            local = self._resolve_local_receiver(file, call, owner)
            if local is not None:
                return local
        return self._resolve_direct(file, call, owner)

    def _resolve_local_receiver(
        self,
        file: FileIR,
        call: CallIR,
        owner: SymbolRef,
    ) -> CallResolution | None:
        parts = call.raw_callee.split(".")
        if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
            return None
        receiver_type = self._local_receiver_type(file, call, owner, parts[0])
        if receiver_type is None:
            return None

        direct = self._methods_for_type(receiver_type, call.callee_name)
        if len(direct) == 1:
            return CallResolution(
                call,
                file.path,
                owner.key,
                "local_receiver",
                (direct[0].key,),
                direct[0].key,
            )
        if len(direct) > 1:
            return CallResolution(
                call,
                file.path,
                owner.key,
                "ambiguous",
                tuple(ref.key for ref in direct),
            )
        mro = self._mro(receiver_type)
        if mro is None:
            return CallResolution(call, file.path, owner.key, "mro_incomplete", ())
        for type_qname in mro[1:]:
            inherited = self._methods_for_type(type_qname, call.callee_name)
            if len(inherited) == 1:
                return CallResolution(
                    call,
                    file.path,
                    owner.key,
                    "local_receiver_inherited",
                    (inherited[0].key,),
                    inherited[0].key,
                )
            if len(inherited) > 1:
                return CallResolution(
                    call,
                    file.path,
                    owner.key,
                    "ambiguous",
                    tuple(ref.key for ref in inherited),
                )
        return CallResolution(call, file.path, owner.key, "unresolved", ())

    def _local_receiver_type(
        self,
        file: FileIR,
        call: CallIR,
        owner: SymbolRef,
        receiver_name: str,
    ) -> str | None:
        state: dict[str, str | None] = {}
        call_position = (call.start_line, call.start_column)
        bindings = sorted(
            (
                binding
                for binding in file.local_bindings
                if binding.owner_qname == call.owner_qname
                and (binding.start_line, binding.start_column) < call_position
            ),
            key=lambda binding: (
                binding.start_line,
                binding.start_column,
                binding.target_name,
            ),
        )
        for binding in bindings:
            if binding.guarded:
                state[binding.target_name] = None
                continue
            annotation_type = self._annotation_type(file, binding.annotation)
            if binding.annotation is not None:
                if annotation_type is None:
                    state[binding.target_name] = None
                elif binding.value_kind == "annotation":
                    state[binding.target_name] = annotation_type
                elif binding.value_kind == "name":
                    value_type = state.get(str(binding.value_name))
                    state[binding.target_name] = (
                        annotation_type if value_type == annotation_type else None
                    )
                elif binding.value_kind == "call":
                    value_type = self._call_result_type(file, binding, owner)
                    state[binding.target_name] = (
                        annotation_type if value_type == annotation_type else None
                    )
                else:
                    state[binding.target_name] = None
                continue
            if binding.value_kind == "name":
                state[binding.target_name] = state.get(str(binding.value_name))
                continue
            if binding.value_kind == "call":
                state[binding.target_name] = self._call_result_type(file, binding, owner)
                continue
            state[binding.target_name] = None
        return state.get(receiver_name)

    def _call_result_type(
        self,
        file: FileIR,
        binding: LocalBindingIR,
        owner: SymbolRef,
    ) -> str | None:
        candidates, _ = self._binding_call_candidates(file, binding, owner)
        type_refs = self._type_candidates(candidates)
        if len(type_refs) == 1:
            return type_refs[0].qname
        if type_refs:
            return None
        factories = self._callable_candidates(candidates)
        if len(factories) != 1 or factories[0].return_annotation is None:
            return None
        factory_file = self.index.file(factories[0].path)
        if factory_file is None:
            return None
        return self._annotation_type(factory_file, factories[0].return_annotation)

    def _binding_call_candidates(
        self,
        file: FileIR,
        binding: LocalBindingIR,
        owner: SymbolRef,
    ) -> tuple[tuple[str, ...], frozenset[str]]:
        candidates: list[str] = []
        imported: set[str] = set()
        if binding.value_qname_hint:
            candidates.append(binding.value_qname_hint)
        if binding.value_name:
            candidates.extend(
                _lexical_candidates(owner.qname, file.module_qname, binding.value_name)
            )
        raw = binding.value_qname_hint or binding.value_name or ""
        root, dot, rest = raw.partition(".")
        bindings = _import_bindings(file)
        if root in bindings:
            target = f"{bindings[root]}.{rest}" if dot else bindings[root]
            candidates.append(target)
            imported.add(target)
        if binding.value_name and binding.value_name in bindings:
            candidates.append(bindings[binding.value_name])
            imported.add(bindings[binding.value_name])
        return tuple(dict.fromkeys(candidates)), frozenset(imported)

    def _annotation_type(self, file: FileIR, annotation: str | None) -> str | None:
        if annotation is None:
            return None
        value = annotation.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1].strip()
        if not _DOTTED_IDENTIFIER.fullmatch(value):
            return None
        candidates = [value]
        bindings = _import_bindings(file)
        root, dot, rest = value.partition(".")
        if root in bindings:
            candidates.append(f"{bindings[root]}.{rest}" if dot else bindings[root])
        if not dot:
            candidates.append(f"{file.module_qname}.{value}")
        refs = self._type_candidates(dict.fromkeys(candidates))
        return refs[0].qname if len(refs) == 1 else None

    def _resolve_direct(self, file: FileIR, call: CallIR, owner: SymbolRef) -> CallResolution:
        candidates, import_candidate = self._direct_candidates(file, call, owner)
        if call.receiver_kind in {"none", "name"}:
            type_refs = self._type_candidates(candidates)
            if len(type_refs) == 1:
                type_ref = type_refs[0]
                initializer = self._initializer_for_type(type_ref.qname)
                return CallResolution(
                    call,
                    file.path,
                    owner.key,
                    "constructor_exact_import"
                    if type_ref.qname in import_candidate
                    else "constructor_exact_local",
                    tuple(ref.key for ref in type_refs),
                    construction_target_key=type_ref.key,
                    initializer_candidate_keys=tuple(ref.key for ref in initializer[0]),
                    initializer_target_key=initializer[1],
                    initializer_status=initializer[2],
                )
            if len(type_refs) > 1:
                return CallResolution(
                    call,
                    file.path,
                    owner.key,
                    "constructor_ambiguous",
                    tuple(ref.key for ref in type_refs),
                )
        candidate_refs = self._callable_candidates(candidates)
        candidate_keys = tuple(ref.key for ref in candidate_refs)
        if len(candidate_refs) == 1:
            status = (
                "exact_import" if candidate_refs[0].qname in import_candidate else "exact_local"
            )
            return CallResolution(
                call, file.path, owner.key, status, candidate_keys, candidate_refs[0].key
            )
        if len(candidate_refs) > 1:
            return CallResolution(call, file.path, owner.key, "ambiguous", candidate_keys)

        if call.receiver_kind == "dynamic" or call.receiver_kind == "attribute":
            return CallResolution(call, file.path, owner.key, "dynamic", ())
        if self._is_import_reference(file, call):
            return CallResolution(call, file.path, owner.key, "external", ())
        return CallResolution(call, file.path, owner.key, "unresolved", ())

    def _type_candidates(self, qnames: Iterable[str]) -> tuple[SymbolRef, ...]:
        candidates = {ref.key: ref for qname in qnames for ref in self.index.types(qname)}
        return tuple(sorted(candidates.values(), key=lambda ref: ref.key))

    def _initializer_for_type(
        self, type_qname: str
    ) -> tuple[tuple[SymbolRef, ...], str | None, str | None]:
        direct = self._methods_for_type(type_qname, "__init__")
        if len(direct) == 1:
            return direct, direct[0].key, "exact_local"
        if len(direct) > 1:
            return direct, None, None

        mro = self._mro(type_qname)
        if mro is None:
            return (), None, None
        for inherited_type in mro[1:]:
            methods = self._methods_for_type(inherited_type, "__init__")
            if len(methods) == 1:
                return methods, methods[0].key, "inherited_method"
            if len(methods) > 1:
                return methods, None, None
        return (), None, None

    def _resolve_receiver_method(
        self,
        file: FileIR,
        call: CallIR,
        owner: SymbolRef,
        *,
        is_super: bool,
    ) -> CallResolution:
        if is_super and not call.raw_callee.startswith("super()."):
            return CallResolution(call, file.path, owner.key, "dynamic", ())

        owner_type = self._owner_type_for_call(owner)
        if owner_type is None:
            return CallResolution(call, file.path, owner.key, "unresolved", ())

        if not is_super:
            direct = self._methods_for_type(owner_type, call.callee_name)
            if len(direct) == 1:
                status = "self_direct" if call.receiver_kind == "self" else "cls_direct"
                return CallResolution(
                    call, file.path, owner.key, status, (direct[0].key,), direct[0].key
                )
            if len(direct) > 1:
                return CallResolution(
                    call, file.path, owner.key, "ambiguous", tuple(ref.key for ref in direct)
                )

        mro = self._mro(owner_type)
        if mro is None:
            return CallResolution(call, file.path, owner.key, "mro_incomplete", ())
        # For both inherited self/cls calls and zero-argument super(), start
        # after the immediate owner class.  Direct self/cls ownership has
        # already been handled above.
        inherited_candidates: list[SymbolRef] = []
        for type_qname in mro[1:]:
            methods = self._methods_for_type(type_qname, call.callee_name)
            if len(methods) == 1:
                inherited_candidates = methods
                break
            if len(methods) > 1:
                return CallResolution(
                    call,
                    file.path,
                    owner.key,
                    "ambiguous",
                    tuple(method.key for method in methods),
                )
        if not inherited_candidates:
            return CallResolution(call, file.path, owner.key, "unresolved", ())
        status = "super_method" if is_super else "inherited_method"
        return CallResolution(
            call,
            file.path,
            owner.key,
            status,
            (inherited_candidates[0].key,),
            inherited_candidates[0].key,
        )

    def _owner_type_for_call(self, owner: SymbolRef) -> str | None:
        if owner.parent_qname and len(self.index.types(owner.parent_qname)) == 1:
            return owner.parent_qname
        return None

    def _direct_candidates(
        self,
        file: FileIR,
        call: CallIR,
        owner: SymbolRef,
    ) -> tuple[tuple[str, ...], frozenset[str]]:
        candidates: list[str] = []
        imported: set[str] = set()
        if call.callee_qname_hint:
            candidates.append(call.callee_qname_hint)
        if call.receiver_kind == "none" and call.callee_name:
            candidates.extend(_lexical_candidates(owner.qname, file.module_qname, call.callee_name))

        raw_parts = call.raw_callee.split(".")
        bindings = _import_bindings(file)
        if raw_parts and raw_parts[0] in bindings:
            target = ".".join([bindings[raw_parts[0]], *raw_parts[1:]])
            candidates.append(target)
            imported.add(target)
        if call.callee_name and call.callee_name in bindings and call.receiver_kind == "none":
            imported.add(bindings[call.callee_name])
            candidates.append(bindings[call.callee_name])

        # Preserve first occurrence (for strategy selection) while avoiding
        # duplicate qnames from the extractor hint and lexical reconstruction.
        return tuple(dict.fromkeys(candidates)), frozenset(imported)

    def _is_import_reference(self, file: FileIR, call: CallIR) -> bool:
        root = call.raw_callee.split(".", maxsplit=1)[0]
        return root in _import_bindings(file)

    def _callable_candidates(self, qnames: Iterable[str]) -> tuple[SymbolRef, ...]:
        candidates = {ref.key: ref for qname in qnames for ref in self.index.callables(qname)}
        return tuple(candidates[key] for key in sorted(candidates))

    def _methods_for_type(self, type_qname: str, method_name: str | None) -> tuple[SymbolRef, ...]:
        if not method_name:
            return ()
        return tuple(
            ref
            for ref in self.index.callables(f"{type_qname}.{method_name}")
            if ref.kind == "method" and ref.parent_qname == type_qname
        )

    def _mro(self, type_qname: str, active: frozenset[str] = frozenset()) -> tuple[str, ...] | None:
        if type_qname in self._mro_cache:
            self._mro_cache.move_to_end(type_qname)
            return self._mro_cache[type_qname]
        state = self.index.base_state(type_qname)
        if type_qname in active or state.incomplete:
            self._cache_mro(type_qname, None)
            return None
        if len(self.index.types(type_qname)) != 1:
            self._cache_mro(type_qname, None)
            return None
        parents = state.bases
        parent_mros: list[tuple[str, ...]] = []
        for parent in parents:
            parent_mro = self._mro(parent, active | {type_qname})
            if parent_mro is None:
                self._cache_mro(type_qname, None)
                return None
            parent_mros.append(parent_mro)
        merged = _c3_merge([*parent_mros, parents])
        if merged is None:
            self._cache_mro(type_qname, None)
            return None
        mro = (type_qname, *merged)
        self._cache_mro(type_qname, mro)
        return mro

    def _cache_mro(self, type_qname: str, value: tuple[str, ...] | None) -> None:
        # Long inheritance chains are valid, but retaining them defeats the
        # exporter memory bound.  They are recomputed on demand instead.
        if value is not None and len(value) > self._mro_cache_max_length:
            return
        self._mro_cache[type_qname] = value
        self._mro_cache.move_to_end(type_qname)
        if len(self._mro_cache) > self._mro_cache_limit:
            self._mro_cache.popitem(last=False)


def _base_states_from_files(files: Iterable[FileIR], index: ResolverIndex) -> dict[str, BaseState]:
    """Build legacy in-memory base state once; SQLite resolves it lazily."""
    bases: dict[str, list[str]] = {}
    incomplete: set[str] = set()
    for file in files:
        bindings = _import_bindings(file)
        for inheritance in file.inheritance:
            children = index.types(inheritance.type_qname)
            if len(children) != 1:
                incomplete.add(inheritance.type_qname)
                continue
            candidate_qnames = _inheritance_candidates(
                inheritance.base_name, inheritance.base_qname, bindings
            )
            parent_refs = {ref.key: ref for qname in candidate_qnames for ref in index.types(qname)}
            if len(parent_refs) != 1:
                incomplete.add(inheritance.type_qname)
                continue
            bases.setdefault(inheritance.type_qname, []).append(
                next(iter(parent_refs.values())).qname
            )
    return {
        qname: BaseState(tuple(values), qname in incomplete) for qname, values in bases.items()
    } | {qname: BaseState((), True) for qname in incomplete if qname not in bases}


def _is_direct_receiver_call(call: CallIR, receiver: str) -> bool:
    """Reject malformed IR that labels a receiver chain as a direct method call."""

    try:
        expression = ast.parse(call.raw_callee, mode="eval").body
    except SyntaxError:
        return False
    return (
        isinstance(expression, ast.Attribute)
        and isinstance(expression.value, ast.Name)
        and expression.value.id == receiver
        and expression.attr == call.callee_name
    )


def _group_by_qname(values: Iterable[SymbolRef]) -> dict[str, tuple[SymbolRef, ...]]:
    grouped: dict[str, list[SymbolRef]] = defaultdict(list)
    for value in values:
        grouped[value.qname].append(value)
    return {qname: tuple(sorted(refs, key=lambda ref: ref.key)) for qname, refs in grouped.items()}


def _lexical_candidates(owner_qname: str, module_qname: str, name: str) -> tuple[str, ...]:
    """Return nearest-to-farthest lexical declarations for a bare call."""

    candidates: list[str] = []
    # A nested declaration has the qname ``owner.<locals>.name``.  Starting
    # with the current callable and walking enclosing function scopes handles
    # both outer()->middle() and middle()->deepest() without inventing a class
    # member qname for a lexical function.
    scope = owner_qname
    while scope.startswith(f"{module_qname}."):
        candidates.append(f"{scope}.<locals>.{name}")
        if ".<locals>." not in scope:
            break
        scope = scope.rsplit(".<locals>.", maxsplit=1)[0]
    candidates.append(f"{module_qname}.{name}")
    return tuple(dict.fromkeys(candidates))


def _import_bindings(file: FileIR) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for import_ir in file.imports:
        if import_ir.name == "*":
            continue
        if import_ir.name == import_ir.module:
            binding = import_ir.alias or import_ir.module.split(".", maxsplit=1)[0]
            bindings.setdefault(binding, binding if import_ir.alias is None else import_ir.module)
            continue
        binding = import_ir.alias or import_ir.name
        bindings[binding] = f"{import_ir.module}.{import_ir.name}"
    return bindings


def _inheritance_candidates(
    base_name: str,
    base_qname: str | None,
    bindings: Mapping[str, str],
) -> tuple[str, ...]:
    candidates: list[str] = []
    if base_qname:
        candidates.append(base_qname)
        root, dot, rest = base_qname.partition(".")
        if dot and root in bindings:
            candidates.append(f"{bindings[root]}.{rest}")
    if base_name in bindings:
        candidates.append(bindings[base_name])
    return tuple(dict.fromkeys(candidates))


def _c3_merge(sequences: Iterable[Iterable[str]]) -> tuple[str, ...] | None:
    pending = [list(sequence) for sequence in sequences if sequence]
    result: list[str] = []
    while pending:
        candidate = next(
            (
                sequence[0]
                for sequence in pending
                if not any(sequence[0] in other[1:] for other in pending)
            ),
            None,
        )
        if candidate is None:
            return None
        result.append(candidate)
        pending = [[item for item in sequence if item != candidate] for sequence in pending]
        pending = [sequence for sequence in pending if sequence]
    return tuple(result)
