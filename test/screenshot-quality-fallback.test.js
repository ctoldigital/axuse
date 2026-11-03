/**
 * The byte-budget fallback in captureScreenshot() must only ever LOWER the
 * JPEG quality it retries at. Before this test the retry was hard-coded to
 * quality 45 no matter what the caller asked for — so a caller that already
 * requested quality:20 (to stay small on purpose) got a retry at 45, a
 * HIGHER quality than requested, which grows the image instead of shrinking
 * it and still reports a quality above what was asked for.
 *
 * No real browser needed: conn.send is faked so this runs in every lane.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { captureScreenshot, DEFAULT_BUDGET_BYTES } from "../src/core/screenshot.js";
import { Codes } from "../src/core/errors.js";

/** Fake JPEG payload of roughly `bytes` on the wire, base64-encoded. */
function fakeImage(bytes) {
  return Buffer.alloc(bytes, 1).toString("base64");
}

function makeConn(bytesByQuality) {
  const calls = [];
  const conn = {
    send: async (method, params) => {
      calls.push({ method, quality: params?.quality });
      if (method === "Runtime.evaluate") return { result: { value: 1 } };
      if (method === "Page.getLayoutMetrics") {
        return { cssVisualViewport: { pageX: 0, pageY: 0, clientWidth: 800, clientHeight: 600 } };
      }
      if (method === "Page.captureScreenshot") {
        const bytes = bytesByQuality(params.quality);
        return { data: fakeImage(bytes) };
      }
      throw new Error(`unexpected CDP call ${method}`);
    },
  };
  return { conn, calls };
}

test("quality fallback never retries ABOVE the quality the caller asked for", async () => {
  // quality:20 is already over budget; the old code retried at a fixed 45
  // (higher than 20) instead of giving up at the quality that was asked for.
  const { conn, calls } = makeConn((q) => (q <= 20 ? 10_000 : 1));
  await assert.rejects(
    () => captureScreenshot(conn, "s1", { quality: 20, budgetBytes: 1_000 }),
    (err) => {
      assert.equal(err.code, Codes.SCREENSHOT_BUDGET_EXCEEDED);
      assert.match(err.message, /even at quality 20\./, err.message);
      return true;
    },
  );
  const shots = calls.filter((c) => c.method === "Page.captureScreenshot");
  assert.equal(shots.length, 1, "no pointless retry when the requested quality is already <= the fallback quality");
  assert.equal(shots[0].quality, 20);
});

test("quality fallback still shrinks a too-large default-quality screenshot", async () => {
  // Default quality (80) is over budget; 45 fits — this is the case the
  // fallback exists for, and it must keep working.
  const { conn, calls } = makeConn((q) => (q === 45 ? 500 : 50_000));
  const result = await captureScreenshot(conn, "s1", { budgetBytes: 1_000 });
  assert.equal(result.quality, 45);
  assert.equal(result.bytes, 500);
  const shots = calls.filter((c) => c.method === "Page.captureScreenshot");
  assert.deepEqual(shots.map((c) => c.quality), [80, 45]);
});

test("default budget is exported and used when none is passed", async () => {
  const { conn } = makeConn(() => 100);
  const result = await captureScreenshot(conn, "s1", {});
  assert.equal(result.budget_bytes, DEFAULT_BUDGET_BYTES);
});
