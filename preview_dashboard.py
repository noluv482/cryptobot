#!/usr/bin/env python3
"""Render the dashboard to a standalone file so a redesign can be SEEN before it ships.

The bot redeploys on every push, so "build it" and "it is live" are the same
event. That makes UI work risky in a way backend work is not: the only way to
look at a change was to ship it to the running site first.

This closes that gap. It takes the REAL _DASHBOARD_HTML — the exact string the
server sends, not the source text, which is the distinction that mattered in
the dashboard outage — and freezes it against a snapshot of the live API
responses, so every screen renders with real balances, real trades and real
candles while touching nothing.

How the freeze works: a shim installed BEFORE any page script replaces
window.fetch with a lookup into embedded snapshot data, stubs EventSource so
the live-update stream does not spin, and makes writes (open/close a trade,
pause the bot) no-ops that report success. The page cannot tell the difference
and cannot reach the network — so tapping around a preview can never move real
paper money or pause the real bot.

Refresh the snapshot with snap.sh on the server, then:
    python preview_dashboard.py -o design/preview.html
"""
import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "design", "snap")

# snapshot file -> the request paths it answers
ROUTES = {
    "status":         ["/status"],
    "manual_status":  ["/manual/status"],
    "sim":            ["/sim"],
    "prices":         ["/prices"],
    "market":         ["/market"],
    "notes":          ["/notes"],
    "iq":             ["/iq"],
    "news":           ["/news"],
    "achievements":   ["/achievements"],
    "alerts":         ["/alerts"],
    "orderflow":      ["/orderflow"],
    "settings":       ["/settings"],
    "auth_status":    ["/auth/status", "/auth"],
    "daily_pnl":      ["/daily_pnl"],
    "hourly_pnl":     ["/hourly_pnl"],
    "history":        ["/history"],
    "candles":        ["/candles"],
    "setup_history":  ["/setup_history"],
    "rejects":        ["/rejects"],
    "exit_attribution": ["/exit_attribution"],
}


def load_snapshot():
    data = {}
    if not os.path.isdir(SNAP):
        print(f"no snapshot at {SNAP} — run snap.sh on the server first", file=sys.stderr)
        return data
    for stem, paths in ROUTES.items():
        p = os.path.join(SNAP, stem + ".json")
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                body = json.load(f)
        except Exception as e:
            print(f"  skip {stem}: {e}", file=sys.stderr)
            continue
        for path in paths:
            data[path] = body
    return data


SHIM = """
<script>
/* PREVIEW SHIM — installed before any page script.
   Freezes the dashboard against a captured snapshot: no network, no writes,
   no way to touch the real bot or the real paper book. */
(function(){
  var SNAP = __SNAPSHOT__;
  var BANNER_SHOWN = false;

  function jsonResponse(body){
    return Promise.resolve({
      ok: true, status: 200,
      json: function(){ return Promise.resolve(body); },
      text: function(){ return Promise.resolve(JSON.stringify(body)); },
    });
  }
  function pathOf(u){
    try { return new URL(u, 'http://x').pathname; } catch(e){ return String(u).split('?')[0]; }
  }

  var realFetch = window.fetch;
  window.fetch = function(input, init){
    var url = (typeof input === 'string') ? input : (input && input.url) || '';
    var p = pathOf(url);
    var method = ((init && init.method) || 'GET').toUpperCase();

    if (method !== 'GET') {
      // Writes are inert here on purpose: this is a look, not a trade.
      flash('Preview - "' + p + '" does nothing here');
      return jsonResponse({ ok: true, preview: true });
    }
    if (SNAP[p] !== undefined) return jsonResponse(SNAP[p]);
    // anything not captured: an empty object beats an exception
    return jsonResponse({});
  };

  // The live stream would retry forever against nothing.
  window.EventSource = function(){
    return { close: function(){}, addEventListener: function(){},
             onmessage: null, onerror: null, readyState: 0 };
  };
  if (navigator.serviceWorker) {
    try { navigator.serviceWorker.register = function(){ return Promise.resolve({}); }; } catch(e){}
  }

  function flash(msg){
    var el = document.getElementById('__preview_toast');
    if (!el) {
      el = document.createElement('div');
      el.id = '__preview_toast';
      el.style.cssText = 'position:fixed;left:50%;bottom:78px;transform:translateX(-50%);'
        + 'background:rgba(10,20,40,.96);border:1px solid rgba(255,179,0,.5);color:#ffb300;'
        + 'font:600 11px/1.4 ui-monospace,monospace;padding:9px 13px;border-radius:9px;'
        + 'z-index:99999;pointer-events:none;max-width:80vw;text-align:center';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(el.__t);
    el.__t = setTimeout(function(){ el.style.opacity = '0'; }, 2200);
  }

  window.addEventListener('DOMContentLoaded', function(){
    var b = document.createElement('div');
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;'
      + 'background:rgba(255,179,0,.14);border-bottom:1px solid rgba(255,179,0,.4);'
      + 'color:#ffb300;font:700 9.5px/1 ui-monospace,monospace;letter-spacing:.12em;'
      + 'text-align:center;padding:5px 0;pointer-events:none';
    b.textContent = 'PREVIEW - FROZEN DATA, NOTHING IS LIVE';
    document.body.appendChild(b);
    document.body.style.paddingTop = '19px';
  });
})();
</script>
"""


def build(out_path):
    import bot_server as bs
    page = bs._DASHBOARD_HTML
    snap = load_snapshot()
    print(f"snapshot: {len(snap)} routes")

    payload = json.dumps(snap).replace("</", "<\\/")
    shim = SHIM.replace("__SNAPSHOT__", payload)

    # Before any page script: the shim must own fetch by the time they run.
    marker = "<style>"
    i = page.find(marker)
    if i < 0:
        raise SystemExit("could not find an injection point in the page")
    out = page[:i] + shim + page[i:]

    with io.open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"wrote {out_path} — {len(out):,} bytes")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(HERE, "design", "preview.html"))
    args = ap.parse_args()
    return build(args.out)


if __name__ == "__main__":
    sys.exit(main())
