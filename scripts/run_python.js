const { spawnSync } = require("node:child_process");

const args = process.argv.slice(2);
const configured = process.env.PYTHON_BIN ? [process.env.PYTHON_BIN] : [];
const candidates = [
  ...configured,
  ...(process.platform === "win32" ? ["py", "python", "python3"] : ["python", "python3", "py"]),
];

let lastError = null;
for (const command of candidates) {
  const result = spawnSync(command, args, { stdio: "inherit", shell: false });
  if (result.error && result.error.code === "ENOENT") {
    lastError = result.error;
    continue;
  }
  if (result.error) {
    lastError = result.error;
    continue;
  }
  process.exit(result.status ?? 0);
}

console.error(
  `No usable Python executable found. Tried: ${candidates.join(", ")}`
);
if (lastError) {
  console.error(lastError.message);
}
process.exit(127);
