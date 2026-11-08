// Syntax-check every browser module without starting the app or a browser.
// `node --check` only parses; it does not resolve imports or touch the DOM.
import { execFileSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { join } from "node:path";

const roots = ["ui/js", "ui/js/views"];
const files = [];
for (const root of roots) {
  for (const name of readdirSync(root)) {
    if (name.endsWith(".js")) files.push(join(root, name));
  }
}

let failed = 0;
for (const file of files.sort()) {
  try {
    execFileSync(process.execPath, ["--check", file], { stdio: "pipe" });
  } catch (err) {
    failed += 1;
    process.stderr.write(`FAIL ${file}\n`);
    process.stderr.write(String(err.stderr || err.message) + "\n");
  }
}

if (failed) {
  console.error(`\n${failed} of ${files.length} JavaScript file(s) failed syntax checks.`);
  process.exit(1);
}
console.log(`JS syntax OK (${files.length} files)`);
