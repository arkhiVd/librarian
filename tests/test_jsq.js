// Frontend escaping gate for strings embedded in inline event handlers.
//
// A browser decodes HTML entities in an inline onclick BEFORE parsing the JS, so
// escaping ' to &#39; does not protect a single-quoted JS string — it terminates it.
// esc() is for markup; jsq() is for anything entering a handler.
//
// Album, artist and file names here come from Lidarr and from filenames on disk, both
// of which are third-party input.

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "..", "app", "static", "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
// Pull the two helpers out of the page itself, so this tests what ships rather than a
// copy that can drift.
const helpers = script.match(/function esc\([\s\S]*?\n\}/)[0] +
  "\n" + script.match(/function jsq\([\s\S]*?\n/)[0];
const sandbox = {};
new Function("g", helpers + "\ng.esc = esc; g.jsq = jsq;")(sandbox);
const { esc, jsq } = sandbox;

let failures = 0;
function check(name, actual, expected) {
  if (actual !== expected) {
    console.error(`FAIL ${name}\n  expected: ${expected}\n  actual:   ${actual}`);
    failures++;
  }
}

// Synthetic hostile metadata that must remain inert.
check("apostrophe via jsq", jsq("WE DON'T TRUST YOU"), "WE DON\\&#39;T TRUST YOU");
check("apostrophe via esc", esc("WE DON'T TRUST YOU"), "WE DON&#39;T TRUST YOU");

// The payload that actually executed there.
check("xss breakout", jsq("');alert('pwned"), "\\&#39;);alert(\\&#39;pwned");

check("backslash is escaped first", jsq("a\\b"), "a\\\\b");
check("tags in markup", esc("<img src=x onerror=alert(1)>"),
  "&lt;img src=x onerror=alert(1)&gt;");
check("quotes in attributes", esc('a"b'), "a&quot;b");
check("ampersand once", esc("Example A & B"), "Example A &amp; B");

// Synthetic path forms: Windows separators and en dashes must survive.
check("en dash untouched", esc("Example – Part 2"), "Example – Part 2");
check("nulls and empties", esc(null) + esc(undefined) + esc(""), "");

// A path with an apostrophe must round-trip through an onclick handler.
const handler = `<button onclick="go('${jsq("Example O'Artist/Album")}')">`;
check("handler stays single-quoted",
  handler, `<button onclick="go('Example O\\&#39;Artist/Album')">`);

if (failures) {
  console.error(`\n${failures} escaping test(s) failed`);
  process.exit(1);
}
console.log("escaping tests passed");
