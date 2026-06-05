# This storefront copy is DEPRECATED.

The canonical storefront for packoasis.com is now:
  ~/Downloads/packoasis-storefront  (repo: yangyangnovelist-hub/packoasis-storefront)

`wrangler.jsonc` has been renamed to `wrangler.jsonc.deprecated` to prevent
accidental `wrangler deploy` runs from this directory that would overwrite
the canonical worker. Do NOT rename it back without coordinating first.

If you need to make storefront changes, make them in `packoasis-storefront`
and deploy from there.
