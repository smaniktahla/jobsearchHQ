# Vendored libraries

| File | Project | Version | License | SHA-256 |
|---|---|---|---|---|
| `html2canvas-pro.min.js` | [html2canvas-pro](https://github.com/yorickshan/html2canvas-pro) | 2.6.3 | MIT (`html2canvas-pro.LICENSE.txt`) | `436d48af523862d2b1e735803e74f355112f76dd330c061e391580b266398944` |

Used by the feedback form's "Capture this page" button; loaded only when someone clicks it. It is kept in the repo (not loaded
from a CDN) so there is no third-party request and it works on a private network.

To update: download `dist/html2canvas-pro.min.js` for the new version from the npm tarball, compare its hash with the jsDelivr copy
of the same version, replace the file, update this table and the `?v=` in `static/index.html`, and run the tests (they check the hash).
