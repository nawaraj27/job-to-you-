/**
 * Copies vendored JS from node_modules into static/vendor so the app can be
 * served without any external CDN (offline-safe, CSP-friendly, versioned with
 * package-lock.json). Run via `npm run vendor`.
 */
import { copyFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// The **CSP build** of Alpine (ADR-A31). The standard build evaluates
// directive expressions with `new Function`, which forces `'unsafe-eval'` into
// the policy; this one parses them into an AST and walks it, so the header no
// longer has to make that exception. Templates must keep every expression to
// what it evaluates — property access, calls, member access, ternaries,
// assignments, comparisons — and put anything statement-shaped in a component
// method instead.
const files = [
  ["node_modules/@alpinejs/csp/dist/cdn.min.js", "static/vendor/alpine.min.js"],
  ["node_modules/htmx.org/dist/htmx.min.js", "static/vendor/htmx.min.js"],
];

for (const [from, to] of files) {
  const dest = resolve(root, to);
  await mkdir(dirname(dest), { recursive: true });
  await copyFile(resolve(root, from), dest);
  console.log(`vendored ${to}`);
}
