#!/usr/bin/env node
"use strict";

// npm's Windows cmd shim runs `node <this file>`, so argv[1] is the file name.
process.env.VIAJANTE_BIN = "viajante";
require("./cli.js");
