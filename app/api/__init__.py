"""The versioned HTTP surface (Phase 1c).

Everything under here is ADDITIVE. The frozen marketplace keeps its routers in
`app.routers` and its response shapes byte-for-byte unchanged; the Engine gets
its own namespace at `/v1`, its own auth model (org-scoped memberships and API
keys rather than the legacy `users.role` string) and its own error envelope.
The two never share a route, a schema or a status-code convention.

    api.auth        who is calling, and may they - Principal + Access
    api.keys        API key minting, hashing and lookup
    api.errors      engine_services errors -> HTTP, scoped to /v1 only
    api.pagination  limit/offset with caps
    api.v1          the routers themselves

`api.errors.install_error_handlers` is the only thing `app.main` needs beyond
the router; see its docstring for why the handlers refuse to touch legacy paths.
"""
