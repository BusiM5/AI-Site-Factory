import assert from "node:assert/strict";
import test from "node:test";
import { formatDuration, parseApiTimestamp, secondsBetween } from "./time.js";

test("legacy timezone-less API timestamps are interpreted as UTC", () => {
  assert.equal(parseApiTimestamp("2026-07-24T02:00:10").toISOString(), "2026-07-24T02:00:10.000Z");
  assert.equal(secondsBetween("2026-07-24T02:00:10", "2026-07-24T02:01:16Z"), 66);
  assert.equal(formatDuration(66), "1:06");
});

test("timezone-aware timestamps retain their explicit offset", () => {
  assert.equal(parseApiTimestamp("2026-07-24T04:00:10+02:00").toISOString(), "2026-07-24T02:00:10.000Z");
});
