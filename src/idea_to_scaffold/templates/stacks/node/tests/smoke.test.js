import { test } from "node:test";
import assert from "node:assert/strict";

import { version } from "../src/index.js";

test("the package loads", () => {
  assert.equal(typeof version, "string");
});
