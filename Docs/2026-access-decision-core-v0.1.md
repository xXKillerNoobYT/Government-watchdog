# ACCESS-2026 v0.1 — Explicit Access Decision Core

Status: implementation contract for review. Merging this slice grants no user
access, changes no route, activates no area, and sets no price.

## Purpose

The existing account tier answers the private-beta lifecycle question:
`approved`, `paused`, or `revoked`. It is not a product entitlement. The new
decision core answers a narrower server-side question:

> May this authenticated account use this exact feature for this exact area and
> publication lane at this instant?

An allow requires five independent facts:

1. the existing account tier is `approved`;
2. exactly one active customer plan or at least one active internal program;
3. an explicit feature and publication-lane grant;
4. an explicit exact-area grant tied to the same active assignment event; and
5. an owner-activated area serving state.

Any missing, future, expired, revoked, unknown, malformed, or ambiguous fact
denies. The evaluator uses a server clock and one coherent read transaction; it
does not cache decisions, accept a client-selected evaluation time, or trust
browser state.

## Deliberate non-implications

- A plan assignment grants no feature.
- A program assignment grants no feature.
- A town grant grants no sibling, county, state, or border town.
- A county/state grant grants no descendants in v0.1.
- A visual Simple/Advanced mode grants nothing.
- An approved beta account receives no product entitlement automatically.
- `area_entitlements` remains an economics/readiness table, not a user grant.
- A feature from one plan/program cannot combine with geography from another.
- Reviewer-internal grants require a `developer` or `beta_tester` program; they
  are not customer-plan benefits.

## Assignment provenance and atomic changes

Every feature and geography row references exactly one active plan or program
assignment event. When that assignment is superseded, revoked, or expires, its
dependent grants stop matching even if another lower-privilege profile remains
active. A downgrade therefore cannot accidentally carry a Global/Beta feature
into Free.

The low-level append helpers never commit. A caller must place the complete
change in one transaction and explicitly supply one `operation_id` plus the
non-empty `actor` responsible for the change. The caller reuses that audit
context across its assignment, feature, and geography rows. Any failure can
then roll back the entire change without exposing a partial plan or a ledger
entry with unknown authorship.

All timestamps are canonical millisecond UTC values. The database rejects
offset, malformed, and invalid dates; the evaluator revalidates and
chronologically sorts them as defense in depth. Decisions include their
evaluation time, catalog version, and exact assignment basis for reproducible
review.

## Stable internal codes versus public copy

`free`, `pro_town`, `pro_multi_home`, `pro_state`, `pro_global`, and `contract`
are internal semantic keys. They are not public names, prices, promises, or an
approved benefit matrix. Internal/special programs remain separate:
`developer`, `beta_tester`, and `special_contract_team`.

## Blocked policy layers

The following require explicit owner review before they can be built on top:

- public product names and the exact feature matrix for each plan/program;
- whether the three-month Multi-Home replacement limit is account-wide or
  per-slot, plus upgrade/downgrade and support-override rules;
- the authoritative border-town adjacency source, version, tie-breaking, and
  the meaning of “up to three” border towns;
- contract/team roles, seat limits, delegation, and audit requirements;
- beta/developer expiry and environment boundaries;
- anonymous Free access and the public session contract;
- pricing, billing, trials, refunds, taxes, and payment-provider behavior.

Until those policies are approved, administrators may create explicit manual
rows for review fixtures or controlled beta tests. No automatic grant expansion
is permitted.

## Integration order

1. Review this inert schema and evaluator.
2. Order/merge the beta-cookie and immutable-release PRs.
3. Add one authenticated HTTP decision/session projection using the composed
   credential gate.
4. Package the access-control import closure into the web artifact.
5. Make the frontend consume only that server response; designed gaps remain
   honest when the server omits a capability.
6. Add versioned policy engines only after their owner decisions are recorded.
