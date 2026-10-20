"""Late-bound seams back into ni_flow for the two things the verify checks need that stay there
(the Library handle and the place resolver): resolved at call time so a test or a caller that
patches ``ni_flow`` still reaches the moved code."""

from __future__ import annotations


def resolve_library() -> object | None:
    """The Library handle, as ``ni_flow._resolve_library`` returns it now."""
    from .. import ni_flow  # late: ni_flow imports this package
    assert hasattr(ni_flow, "_resolve_library"), "ni_flow owns the Library handle"
    return ni_flow._resolve_library()


def place_states_via_resolver(place: str, request: str) -> set[str]:
    """Every US state the place could land in, as ``ni_flow._place_states_via_resolver`` reads it now."""
    assert isinstance(place, str) and isinstance(request, str), "args required"
    from .. import ni_flow  # late: ni_flow imports this package
    return ni_flow._place_states_via_resolver(place, request)
