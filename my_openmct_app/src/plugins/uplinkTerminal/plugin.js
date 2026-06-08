const TEMPLATE = `
<div class="uplink-panel">
  <div class="uplink-header">
    <span class="uplink-title">&#x2B06; Uplink Terminal</span>
    <span class="uplink-status-dot" title="Disconnected">&#x25CF;</span>
  </div>

  <div class="uplink-form">
    <div class="up-form-row">
      <label class="up-label">Command Class</label>
      <select id="up-class" class="up-select">
        <option value="ROUTE_UPDATE">ROUTE_UPDATE</option>
        <option value="RECOVERY">RECOVERY</option>
        <option value="MODE">MODE</option>
        <option value="DIAGNOSTIC">DIAGNOSTIC</option>
        <option value="CONFIG">CONFIG</option>
        <option value="VIEWPOINT">VIEWPOINT</option>
      </select>
    </div>

    <div id="up-route-section">
      <div class="up-form-row">
        <label class="up-label">Route Type</label>
        <select id="up-route-type" class="up-select">
          <option value="1">MISSION_EXTENSION</option>
          <option value="2">LANDING</option>
        </select>
      </div>
      <div class="up-form-row">
        <label class="up-label">Waypoints (X Y Z metres)</label>
        <button id="up-add-wp" class="up-btn-xs">+ Add</button>
      </div>
      <div id="up-waypoints"></div>
    </div>

    <div id="up-generic-section" style="display:none">
      <div class="up-form-row">
        <label class="up-label">Payload hex (optional)</label>
        <input type="text" id="up-payload-hex" class="up-text-input" placeholder="e.g. 0102AABB">
      </div>
    </div>

    <div class="up-form-row" style="margin-top:8px">
      <button id="up-send" class="up-btn-send">&#x25BA; Send</button>
      <button id="up-clear-log" class="up-btn-xs">Clear log</button>
    </div>
  </div>

  <div class="uplink-log-header">Command Log</div>
  <div class="uplink-log"></div>
</div>
`;

const STYLES = `
.uplink-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 8px;
  box-sizing: border-box;
  background: #1a1a2e;
  color: #e0e0e0;
  font-family: monospace;
  overflow: hidden;
}
.uplink-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 8px;
  border-bottom: 1px solid #333;
  margin-bottom: 8px;
}
.uplink-title { font-weight: bold; font-size: 13px; color: #90caf9; }
.uplink-status-dot { color: #ef5350; font-size: 18px; line-height: 1; }
.uplink-form {
  background: #16213e;
  border: 1px solid #333;
  border-radius: 4px;
  padding: 8px;
  margin-bottom: 8px;
  flex-shrink: 0;
}
.up-form-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.up-label { color: #90a4ae; font-size: 11px; min-width: 170px; }
.up-select, .up-text-input, .up-num-input {
  background: #0f3460;
  color: #e0e0e0;
  border: 1px solid #444;
  border-radius: 3px;
  padding: 3px 6px;
  font-family: monospace;
  font-size: 12px;
}
.up-text-input { width: 180px; }
.up-num-input  { width: 72px; }
.up-wp-row {
  display: flex;
  gap: 4px;
  margin-bottom: 4px;
  padding-left: 178px;
  align-items: center;
}
.up-btn-send {
  background: #1565c0;
  color: #fff;
  border: none;
  border-radius: 4px;
  padding: 6px 18px;
  cursor: pointer;
  font-family: monospace;
  font-weight: bold;
  font-size: 12px;
}
.up-btn-send:hover { background: #1976d2; }
.up-btn-xs {
  background: #37474f;
  color: #e0e0e0;
  border: none;
  border-radius: 3px;
  padding: 3px 8px;
  cursor: pointer;
  font-family: monospace;
  font-size: 11px;
}
.up-btn-xs:hover { background: #546e7a; }
.up-btn-danger { background: #7f0000 !important; }
.up-btn-danger:hover { background: #b71c1c !important; }
.uplink-log-header {
  font-size: 11px;
  color: #90a4ae;
  padding: 4px 0;
  border-top: 1px solid #333;
  flex-shrink: 0;
}
.uplink-log {
  flex: 1;
  overflow-y: auto;
  font-size: 11px;
  font-family: monospace;
  background: #0a0a1a;
  border: 1px solid #222;
  border-radius: 3px;
  padding: 4px;
  min-height: 80px;
}
.uplink-log-line { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
.uplink-log-system { color: #78909c; }
.uplink-log-tx     { color: #4fc3f7; }
.uplink-log-rx     { color: #81c784; }
.uplink-log-error  { color: #ef5350; }
`;

let _stylesInjected = false;

function injectStyles() {
    if (_stylesInjected) return;
    _stylesInjected = true;
    const el = document.createElement('style');
    el.textContent = STYLES;
    document.head.appendChild(el);
}

export default function uplinkTerminalPlugin() {
    return function install(openmct) {
        const OBJECT = {
            identifier: { namespace: 'cfs-uplink', key: 'terminal' },
            name: 'Uplink Terminal',
            type: 'uplink-terminal',
            location: 'ROOT'
        };

        openmct.types.addType('uplink-terminal', {
            name: 'Uplink Terminal',
            description: 'Send commands to aircraft via LoRa serial',
            cssClass: 'icon-telemetry',
            creatable: false
        });

        openmct.objects.addRoot(OBJECT.identifier);

        openmct.objects.addProvider('cfs-uplink', {
            get: async function (identifier) {
                return identifier.key === 'terminal' ? OBJECT : undefined;
            }
        });

        openmct.views.addProvider({
            key: 'uplink-terminal-view',
            name: 'Uplink Terminal',
            cssClass: 'icon-telemetry',

            canView(domainObject) {
                return domainObject.type === 'uplink-terminal';
            },

            view(domainObject) {
                let ws        = null;
                let logEl     = null;
                let statusEl  = null;
                let destroyed = false;

                function addLog(type, text) {
                    if (!logEl) return;
                    const line = document.createElement('div');
                    line.className = `uplink-log-line uplink-log-${type}`;
                    const ts = new Date().toISOString().substring(11, 23);
                    line.textContent = `[${ts}] ${text}`;
                    logEl.appendChild(line);
                    logEl.scrollTop = logEl.scrollHeight;
                }

                function setStatus(connected) {
                    if (!statusEl) return;
                    statusEl.style.color = connected ? '#4caf50' : '#ef5350';
                    statusEl.title       = connected ? 'Connected' : 'Disconnected';
                }

                function connect() {
                    if (destroyed) return;
                    if (ws && (ws.readyState === WebSocket.CONNECTING ||
                               ws.readyState === WebSocket.OPEN)) return;

                    ws = new WebSocket('ws://127.0.0.1:8766');

                    ws.onopen = () => {
                        setStatus(true);
                        addLog('system', 'Connected to uplink server (ws://127.0.0.1:8766)');
                    };

                    ws.onclose = () => {
                        setStatus(false);
                        addLog('system', 'Disconnected — retrying in 3 s…');
                        if (!destroyed) setTimeout(connect, 3000);
                    };

                    ws.onerror = () => addLog('error', 'WebSocket error');

                    ws.onmessage = (e) => {
                        try {
                            const d = JSON.parse(e.data);
                            if (d.ok) {
                                addLog('rx', `ACK  seq=${d.seq}  class=${d.class}  frame=${d.frame}`);
                            } else {
                                addLog('error', `NACK  ${d.error}  (seq=${d.seq != null ? d.seq : '?'})`);
                            }
                        } catch (_) {
                            addLog('error', `bad response: ${e.data}`);
                        }
                    };
                }

                return {
                    show(element) {
                        injectStyles();
                        element.innerHTML = TEMPLATE;

                        logEl    = element.querySelector('.uplink-log');
                        statusEl = element.querySelector('.uplink-status-dot');

                        const classSelect    = element.querySelector('#up-class');
                        const routeSection   = element.querySelector('#up-route-section');
                        const genericSection = element.querySelector('#up-generic-section');
                        const waypointsEl    = element.querySelector('#up-waypoints');

                        classSelect.addEventListener('change', () => {
                            const isRoute = classSelect.value === 'ROUTE_UPDATE';
                            routeSection.style.display   = isRoute ? '' : 'none';
                            genericSection.style.display = isRoute ? 'none' : '';
                        });

                        element.querySelector('#up-add-wp').addEventListener('click', () => {
                            const row = document.createElement('div');
                            row.className = 'up-wp-row';
                            row.innerHTML =
                                `<input type="number" step="0.01" placeholder="X m" class="up-wp-x up-num-input">` +
                                `<input type="number" step="0.01" placeholder="Y m" class="up-wp-y up-num-input">` +
                                `<input type="number" step="0.01" placeholder="Z m" class="up-wp-z up-num-input">` +
                                `<button class="up-btn-xs up-btn-danger up-wp-del">✕</button>`;
                            row.querySelector('.up-wp-del').addEventListener('click', () => row.remove());
                            waypointsEl.appendChild(row);
                        });

                        element.querySelector('#up-clear-log').addEventListener('click', () => {
                            if (logEl) logEl.innerHTML = '';
                        });

                        element.querySelector('#up-send').addEventListener('click', () => {
                            const cls = classSelect.value;
                            const msg = { class: cls };

                            if (cls === 'ROUTE_UPDATE') {
                                const rows = waypointsEl.querySelectorAll('.up-wp-row');
                                const wps  = [];
                                for (const row of rows) {
                                    const x = parseFloat(row.querySelector('.up-wp-x').value);
                                    const y = parseFloat(row.querySelector('.up-wp-y').value);
                                    const z = parseFloat(row.querySelector('.up-wp-z').value);
                                    if ([x, y, z].some(Number.isNaN)) {
                                        addLog('error', 'Invalid waypoint — fill all X/Y/Z fields');
                                        return;
                                    }
                                    wps.push({ x, y, z });
                                }
                                if (!wps.length) {
                                    addLog('error', 'Add at least one waypoint before sending');
                                    return;
                                }
                                msg.route_type    = parseInt(element.querySelector('#up-route-type').value, 10);
                                msg.route_version = 1;
                                msg.waypoints     = wps;
                            } else {
                                const hex = element.querySelector('#up-payload-hex').value.trim().replace(/\s+/g, '');
                                if (hex) msg.payload_hex = hex;
                            }

                            if (!ws || ws.readyState !== WebSocket.OPEN) {
                                addLog('error', 'Not connected to uplink server');
                                return;
                            }

                            const preview = cls === 'ROUTE_UPDATE'
                                ? `wps=${msg.waypoints.length}  route_type=${msg.route_type}`
                                : `payload_hex=${msg.payload_hex || '(empty)'}`;
                            addLog('tx', `SEND ${cls}  ${preview}`);
                            ws.send(JSON.stringify(msg));
                        });

                        connect();
                    },

                    destroy() {
                        destroyed = true;
                        if (ws) {
                            ws.onclose = null;
                            ws.close();
                            ws = null;
                        }
                    },

                    priority() { return 1; }
                };
            }
        });
    };
}
