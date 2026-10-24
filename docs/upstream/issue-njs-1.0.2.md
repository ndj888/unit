Title: Update contrib njs to 1.0.2

`pkg/contrib/src/njs/version` pins njs 1.0.1. [njs 1.0.2](https://github.com/nginx/njs/releases/tag/1.0.2) was released on 2026-10-14.

**Why it matters for Unit.** The contrib recipe builds njs with `--no-quickjs`, so Unit uses the njs engine for templates and uploaded `js_module` code. The [1.0.2 CHANGES](https://github.com/nginx/njs/blob/1.0.2/CHANGES) fix several memory-safety bugs in that engine:

- memory corruption in RegExp `replace()` and `split()` with custom `exec()` results (nginx/njs#1090–#1093, #1096, #1097);
- out-of-bounds reads in RegExp replacement and named capture lookup (#1088);
- invalid memory accesses in `Object.values()`/`Object.entries()` on typed arrays (#1098) and in `Object.defineProperty()` with reentrant descriptors (#1089);
- a stack overflow when compiling deeply nested expressions;
- crashes when importing modules after a failed module compilation.

Each of these needs a particular code shape in the configured JavaScript, so this is hardening rather than a known exploitable issue in Unit.

**XML advisories.** CVE-2026-101025 (libxml2 parser context leak when `xml.parse()` fails, fixed in 1.0.2) and the 1.0.2 XML entity attribute fix do not affect the contrib build, which uses `--no-libxml2`. They can matter to a build that links a system libnjs with XML support through `./configure --njs`: njs resolves built-in modules before Unit's module loader in `src/nxt_js.c` runs, so configured `js_module` code could `import xml`. This conclusion comes from reading the source; I did not test it at runtime.

**Suggested change.** Set `NJS_VERSION := 1.0.2`, update `pkg/contrib/src/njs/SHA512SUMS`, and add the tarball to packages.freeunit.org. 1.0.2 also changes XML node mutation semantics, which affects only XML-enabled builds.

Suggested labels: `z-enhancement ⬆️`, `z-packages`.
