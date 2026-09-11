#!/usr/bin/env node
"use strict";

const { spawn } = require("node:child_process");
const { basename } = require("node:path");
const { version } = require("../package.json");

const name = basename(process.argv[1] || "");
const mcp = name !== "viajante";
const spec = mcp ? `viajante[mcp]==${version}` : `viajante==${version}`;
const bin = mcp ? "viajante-mcp" : "viajante";
const child = spawn("uvx", ["--from", spec, bin, ...process.argv.slice(2)], {
  stdio: "inherit",
});
child.on("error", (err) => {
  if (err && err.code === "ENOENT") {
    console.error(
      "viajante needs uv on PATH (and Python 3.10+). Install: https://docs.astral.sh/uv/",
    );
    process.exit(1);
  }
  throw err;
});
child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
