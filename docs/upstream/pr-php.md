Title: feat(php): expose the rewritten path as UNIT_REWRITTEN_PATH

### Proposed changes

A route `rewrite` changes the path that Unit uses to find the script, but a PHP application only sees `REQUEST_URI`, which keeps the original request target. An application that routes on the rewritten path has no way to read it. See nginx/unit#1615.

This adds `$_SERVER['UNIT_REWRITTEN_PATH']`, which holds the request path after any rewrite, without the query. `REQUEST_URI` is unchanged. The name is one of the interned fixed `$_SERVER` names from 25032776, so the array size hint counts it.

The query string tests also check `REQUEST_URI` and `UNIT_REWRITTEN_PATH`, with and without a rewrite and a query.

The ndj888/unit maintenance fork has shipped this variable since June 2026. This PR ports it to the current PHP module.

### Checklist
- [x] I have read [CONTRIBUTING.md](/CONTRIBUTING.md)
- [x] If applicable, I have added tests
- [x] If applicable, I have updated documentation (`CHANGES`, `docs/changes.xml`)

### Testing

CI on a copy of this branch against master 4beef78f (ndj888/unit#2):
- `test (php-8.3)`, `test (php-8.4)`, `test (php-8.5)`, `sanitize (php)`, `ast-grep baseline`, `require a test for a src/ change` and `tsan` pass.
- 38 of 39 checks pass. `c-tests-almalinux8` timed out while installing packages, before any build step.

Suggested labels: `z-enhancement ⬆️`, `z-php`.
