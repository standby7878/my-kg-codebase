"""Snapshot-local static call resolution for the CodeKG Python IR.

The resolver intentionally has no Neo4j dependency.  It operates solely on
the ``RepositoryIR`` currently being loaded and list-valued symbol indexes, so
that duplicate qualified names remain visible as ambiguity rather than being
silently overwritten.  A resolution is a fact about one syntactic call site;
the loader turns successful resolutions into graph relationships.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from codekg.ir import CallIR, FileIR, RepositoryIR

EXACT_RESOLUTION_STATUSES = frozenset(
    {
        "exact_local",
        "exact_import",
        "self_direct",
        "cls_direct",
        "inherited_method",
        "super_method",
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


@dataclass(frozen=True)
class CallResolution:
    """The resolver's complete, lossless conclusion for a call site."""

    call: CallIR
    path: str
    owner_key: str | None
    status: str
    candidate_keys: tuple[str, ...]
    target_key: str | None = None

    @property
    def is_exact(self) -> bool:
        return self.status in EXACT_RESOLUTION_STATUSES and self.target_key is not None


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
        repo,
        owners_by_file_qname=owners_by_file_qname,
        callables=callables,
        types=types,
    )
    return tuple(resolver.resolve(file, call) for file in repo.files for call in file.calls)


class _Resolver:
    def __init__(
        self,
        repo: RepositoryIR,
        *,
        owners_by_file_qname: Mapping[tuple[str, str], Iterable[SymbolRef]],
        callables: Iterable[SymbolRef],
        types: Iterable[SymbolRef],
    ) -> None:
        self.repo = repo
        self.owners_by_file_qname = {
            key: tuple(sorted(values, key=lambda value: value.key))
            for key, values in owners_by_file_qname.items()
        }
        self.callables_by_qname: dict[str, tuple[SymbolRef, ...]] = _group_by_qname(callables)
        self.types_by_qname: dict[str, tuple[SymbolRef, ...]] = _group_by_qname(types)
        self.file_by_path = {file.path: file for file in repo.files}
        self._bases, self._incomplete_types = self._resolved_bases()
        self._mro_cache: dict[str, tuple[str, ...] | None] = {}

    def resolve(self, file: FileIR, call: CallIR) -> CallResolution:
        owners = self.owners_by_file_qname.get((file.path, call.owner_qname), ())
        if not owners:
            return CallResolution(call, file.path, None, "owner_unresolved", ())
        if len(owners) != 1:
            return CallResolution(
                call, file.path, None, "owner_ambiguous", tuple(owner.key for owner in owners)
            )

        owner = owners[0]
        if call.receiver_kind in {"self", "cls"}:
            return self._resolve_receiver_method(file, call, owner, is_super=False)
        if call.receiver_kind == "super":
            return self._resolve_receiver_method(file, call, owner, is_super=True)
        return self._resolve_direct(file, call, owner)

    def _resolve_direct(self, file: FileIR, call: CallIR, owner: SymbolRef) -> CallResolution:
        candidates, import_candidate = self._direct_candidates(file, call, owner)
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
        if owner.parent_qname and len(self.types_by_qname.get(owner.parent_qname, ())) == 1:
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
        candidates = {
            ref.key: ref for qname in qnames for ref in self.callables_by_qname.get(qname, ())
        }
        return tuple(candidates[key] for key in sorted(candidates))

    def _methods_for_type(self, type_qname: str, method_name: str | None) -> tuple[SymbolRef, ...]:
        if not method_name:
            return ()
        return tuple(
            ref
            for ref in self.callables_by_qname.get(f"{type_qname}.{method_name}", ())
            if ref.kind == "method" and ref.parent_qname == type_qname
        )

    def _resolved_bases(self) -> tuple[dict[str, tuple[str, ...]], set[str]]:
        bases: dict[str, tuple[str, ...]] = {}
        incomplete: set[str] = set()
        for file in self.repo.files:
            bindings = _import_bindings(file)
            for inheritance in file.inheritance:
                children = self.types_by_qname.get(inheritance.type_qname, ())
                if len(children) != 1:
                    incomplete.add(inheritance.type_qname)
                    continue
                candidate_qnames = _inheritance_candidates(
                    inheritance.base_name, inheritance.base_qname, bindings
                )
                parent_refs = {
                    ref.key: ref
                    for qname in candidate_qnames
                    for ref in self.types_by_qname.get(qname, ())
                }
                if len(parent_refs) != 1:
                    incomplete.add(inheritance.type_qname)
                    continue
                bases.setdefault(inheritance.type_qname, ())
                parent_qname = next(iter(parent_refs.values())).qname
                bases[inheritance.type_qname] = (*bases[inheritance.type_qname], parent_qname)
        return bases, incomplete

    def _mro(self, type_qname: str, active: frozenset[str] = frozenset()) -> tuple[str, ...] | None:
        if type_qname in self._mro_cache:
            return self._mro_cache[type_qname]
        if type_qname in active or type_qname in self._incomplete_types:
            self._mro_cache[type_qname] = None
            return None
        if len(self.types_by_qname.get(type_qname, ())) != 1:
            self._mro_cache[type_qname] = None
            return None
        parents = self._bases.get(type_qname, ())
        parent_mros: list[tuple[str, ...]] = []
        for parent in parents:
            parent_mro = self._mro(parent, active | {type_qname})
            if parent_mro is None:
                self._mro_cache[type_qname] = None
                return None
            parent_mros.append(parent_mro)
        merged = _c3_merge([*parent_mros, parents])
        if merged is None:
            self._mro_cache[type_qname] = None
            return None
        mro = (type_qname, *merged)
        self._mro_cache[type_qname] = mro
        return mro


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
