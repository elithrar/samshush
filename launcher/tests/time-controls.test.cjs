const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "time-controls.js"), "utf8");
const mailbox = "wgt-private/quiet-hours.";
const session = "a".repeat(32);

function widget() {
  const files = new Map();
  const timers = new Map();
  let timerId = 0;
  const operations = [];
  const filesystem = {
    openFile(file, mode) {
      operations.push([file, mode]);
      if (mode === "w" || (mode === "a" && !files.has(file))) files.set(file, "");
      assert.ok(files.has(file));
      return {
        close() {},
        readString(count) { assert.equal(mode, "r"); return files.get(file).slice(0, count); },
        writeString(text) { assert.equal(mode, "w"); files.set(file, text); }
      };

    }
  };
  const window = {
    crypto: webcrypto, tizen: { filesystem },
    setTimeout(cb, ms) { timers.set(++timerId, { cb, ms }); return timerId; },
    clearTimeout(id) { timers.delete(id); }
  };
  vm.runInNewContext(source, { window, Uint8Array });
  const runTimer = ms => {
    const entry = [...timers.entries()].find(([, timer]) => timer.ms === ms);
    assert.ok(entry, "timer " + ms); timers.delete(entry[0]); entry[1].cb();
  };
  return {
    schedule: window.LauncherSchedule, filesystem, files, operations, timers,
    request: () => files.get(mailbox + "request").trimEnd().split(" "),
    reply: line => { files.set(mailbox + "reply", line); runTimer(250); },
    expire: () => runTimer(12000)
  };
}

test("GET preserves the app's reply file, waits through stale or partial replies and accepts actual minutes", async () => {
  const w = widget();
  const old = "1".repeat(32) + " " + session + " ok 60 120\n";
  w.files.set(mailbox + "reply", old);
  const pending = w.schedule.read();
  const [command, token] = w.request();
  assert.equal(command, "get");
  assert.match(token, /^[a-f0-9]{32}$/);
  assert.deepEqual(w.operations[0], [mailbox + "reply", "a"]);
  assert.equal(w.files.get(mailbox + "reply"), old);
  await assert.rejects(w.schedule.read(), /unavailable/);
  w.reply(token + " " + session + " ok 1255 ");
  w.reply(token + " " + session + " ok 00 440\n");
  w.reply(token + " " + session + " ok 440 440\n");
  w.reply(token + " " + session + " ok 1440 440\n");
  w.reply(token + " " + session + " ok 1255 440\n\n");
  w.reply(token + " " + session + " ok 1255 440\n");
  assert.deepEqual({...await pending}, { session, start: 1255, end: 440 });
  assert.equal(w.timers.size, 0);
});

test("SET sends the last acknowledged session and both old values; checks echoed readback", async t => {
  const previous = { session, start: 1375, end: 440 };
  const next = { start: 1435, end: 440 };
  for (const [name, result, replySession, start, end] of [
    ["saved", "ok", session, 1435, 440],
    ["conflict", "conflict", session, 0, 500],
    ["restarted", "ok", "b".repeat(32), 1435, 440],
    ["mismatch", "ok", session, 1375, 440]
  ]) await t.test(name, async () => {
    const w = widget();
    const pending = w.schedule.set(previous, next);
    const [, token] = w.request();
    assert.deepEqual(w.request(), ["set", token, session, "1375", "440", "1435", "440"]);
    w.reply([token, replySession, result, start, end].join(" ") + "\n");
    if (name === "saved") assert.deepEqual({...await pending}, { session, ...next });
    else await assert.rejects(pending, /could not confirm that change/);
    assert.equal(w.timers.size, 0);
  });
});

test("timeout permits a fresh GET and late replies cannot satisfy it", async () => {
  const w = widget();
  const first = w.schedule.read();
  const firstToken = w.request()[1];
  w.expire();
  await assert.rejects(first, /could not confirm that change/);
  const next = w.schedule.read();
  const secondToken = w.request()[1];
  assert.notEqual(firstToken, secondToken);
  w.reply(firstToken + " " + session + " ok 100 200\n");
  w.reply(secondToken + " " + session + " ok 300 400\n");
  assert.equal((await next).start, 300);
});

test("invalid pairs or a short mailbox write fail without waiting for acknowledgment", async () => {
  const w = widget();
  await assert.rejects(w.schedule.set({ session, start: 60, end: 100 }, {start: 100, end: 100}), /different/);
  assert.equal(w.operations.length, 0);
  const realOpen = w.filesystem.openFile;
  w.filesystem.openFile = (p, mode) => mode === "w" ? {close() {}, writeString(line) { w.files.set(p, line.slice(0, -1)); }} : realOpen(p, mode);
  await assert.rejects(w.schedule.read(), /could not confirm that change/);
  assert.equal(w.files.get(mailbox + "request").endsWith("\n"), false);
  assert.equal(w.timers.size, 0);
});
