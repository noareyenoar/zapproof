# Token and mechanism coverage

**Phase 0 seven-day census (an earlier window, not the settlements below): 0.10% of settlements used an unsupported token.**

| Token | Settlements | Decimals |
|---|---|---|
| `0x833589fcd6edb6e08f4c7c32d4f71b54bda02913` | 2,389,053 | 6 |

| Mechanism | Settlements |
|---|---|
| `eip3009_authorization_used` | 2,389,053 |

The supported list comes from the settlements table and can only contain tokens the decoder already decoded. The unresolved share therefore comes from the Phase 0 census, which scanned without an address filter and recorded what could not be resolved. Measuring it from the settlements table would make it zero by construction, which is what an earlier version did.
