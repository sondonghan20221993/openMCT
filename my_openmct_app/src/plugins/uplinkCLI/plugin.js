import './terminal.css';

const DEFAULT_SERVER = 'http://127.0.0.1:8082';

const CFS_CORE_PARAMS = [
    'attitude_timeout_ms',
    'local_timeout_ms',
    'gps_timeout_ms',
    'ekf_timeout_ms',
    'bridge_timeout_ms',
    'publish_period_ms',
];

const MAVLINK_BRIDGE_PARAMS = [
    'attitude_interval_us',
    'local_position_interval_us',
    'global_position_interval_us',
    'gps_raw_interval_us',
    'ekf_status_interval_us',
    'reconnect_interval_ms',
    'heartbeat_interval_ms',
];

const HELP = {
    general: [
        'Commands:',
        '  config <scope> <param> <value>      Send CONFIG command to uplink_app',
        '  route <mission|landing> x,y,z ...   Send ROUTE_UPDATE command to uplink_app',
        '  recovery [payload_hex]              Send RECOVERY command (payload currently ignored by uplink_app)',
        '  uplinktest                          Check uplink server health and available params',
        '  help [config|route|recovery]        Show detailed help',
        '  clear                               Clear terminal',
        '',
        'Scopes: cfs_core, mavlink_bridge',
    ].join('\n'),

    config: [
        'config <scope> <param> <value>',
        '  scope : cfs_core | mavlink_bridge',
        '  value : uint32 (0 – 4294967295)',
        '',
        '  cfs_core params:',
        '    ' + CFS_CORE_PARAMS.join(', '),
        '',
        '  mavlink_bridge params:',
        '    ' + MAVLINK_BRIDGE_PARAMS.join(', '),
        '',
        '  Example: config cfs_core publish_period_ms 100',
    ].join('\n'),

    route: [
        'route <route_type> x,y,z [x,y,z ...]',
        '  route_type : mission | landing',
        '  waypoint   : x,y,z in meters, LOCAL_NED (Z = AGL, positive up)',
        '  count      : 1 – 16 waypoints',
        '',
        '  Validation is performed by uplink_app (spec §18.4.6.2):',
        '    finite coords, altitude 2–8 m, adjacent 3D distance 2–2 m, flyable area.',
        '  Out-of-range routes are rejected on the drone and the active route is kept.',
        '',
        '  Example: route mission 0,-10,3 2,-10,3',
        '  Example: route landing 2,-8,4 2,-8,2',
    ].join('\n'),

    recovery: [
        'recovery [payload_hex]',
        '  payload_hex : optional raw bytes in hex (future use)',
        '',
        '  uplink_app currently forwards RECOVERY to cfs_core via RECOVERY_CMD_MID (0x190C).',
        '  The payload field is reserved for future action codes and is currently ignored.',
        '',
        '  Example: recovery',
        '  Example: recovery 0102AB  (raw override)',
    ].join('\n'),
};

export default function uplinkCLIPlugin(serverUrl = DEFAULT_SERVER) {
    return function install(openmct) {
        openmct.types.addType('uplink.terminal', {
            name: 'Uplink CLI',
            description: 'CLI terminal for sending uplink commands to cFS uplink_app',
            cssClass: 'icon-telemetry',
        });

        openmct.objectViews.addProvider({
            name: 'Uplink CLI',
            key: 'uplink-cli-view',
            cssClass: 'icon-telemetry',

            canView(domainObject) {
                return domainObject.type === 'uplink.terminal';
            },

            view(domainObject) {
                let outputEl = null;
                let inputEl = null;

                function appendLine(text, cls) {
                    const line = document.createElement('div');
                    line.className = 'uplink-line' + (cls ? ' ' + cls : '');
                    line.textContent = text;
                    outputEl.appendChild(line);
                    outputEl.scrollTop = outputEl.scrollHeight;
                }

                async function dispatch(raw) {
                    const line = raw.trim();
                    if (!line) return;

                    appendLine('> ' + line, 'uplink-cmd');

                    const parts = line.split(/\s+/);
                    const cmd = parts[0].toLowerCase();

                    if (cmd === 'clear') {
                        outputEl.innerHTML = '';
                        return;
                    }

                    if (cmd === 'help') {
                        const topic = parts[1]?.toLowerCase();
                        appendLine(HELP[topic] || HELP.general, 'uplink-info');
                        return;
                    }

                    if (cmd === 'config') {
                        const [, scope, param, valueStr] = parts;
                        if (!scope || !param || valueStr === undefined) {
                            appendLine('[ERR] usage: config <scope> <param> <value>', 'uplink-err');
                            return;
                        }
                        const value = Number(valueStr);
                        if (!Number.isInteger(value) || value < 0 || value > 0xFFFFFFFF) {
                            appendLine('[ERR] value must be a uint32 integer (0 – 4294967295)', 'uplink-err');
                            return;
                        }
                        try {
                            const res = await fetch(`${serverUrl}/api/uplink/config`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ scope, param, value }),
                            });
                            const json = await res.json();
                            if (json.ok) {
                                appendLine(
                                    `[OK] CONFIG accepted  seq=${json.seq}  ${json.scope}.${json.param}=${json.value}`,
                                    'uplink-ok',
                                );
                            } else {
                                const hint = json.available
                                    ? `  available: ${json.available.join(', ')}`
                                    : '';
                                appendLine(`[ERR] ${json.error}${hint}`, 'uplink-err');
                            }
                        } catch (e) {
                            appendLine(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'uplink-err');
                        }
                        return;
                    }

                    if (cmd === 'route') {
                        // route <mission|landing> x,y,z [x,y,z ...]
                        const routeType = parts[1]?.toLowerCase();
                        const wpTokens = parts.slice(2);
                        if (!routeType || wpTokens.length === 0) {
                            appendLine('[ERR] usage: route <mission|landing> x,y,z [x,y,z ...]', 'uplink-err');
                            return;
                        }
                        if (routeType !== 'mission' && routeType !== 'landing') {
                            appendLine('[ERR] route_type must be mission | landing', 'uplink-err');
                            return;
                        }
                        if (wpTokens.length > 16) {
                            appendLine('[ERR] too many waypoints (max 16)', 'uplink-err');
                            return;
                        }
                        const waypoints = [];
                        for (const tok of wpTokens) {
                            const nums = tok.split(',').map(Number);
                            if (nums.length !== 3 || nums.some((n) => !Number.isFinite(n))) {
                                appendLine(`[ERR] bad waypoint '${tok}', expected x,y,z`, 'uplink-err');
                                return;
                            }
                            waypoints.push(nums);
                        }
                        try {
                            const res = await fetch(`${serverUrl}/api/uplink/route`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ route_type: routeType, waypoints }),
                            });
                            const json = await res.json();
                            if (json.ok) {
                                appendLine(
                                    `[OK] ROUTE sent  seq=${json.seq}  ${json.route_type}  wps=${json.waypoint_count}`,
                                    'uplink-ok',
                                );
                            } else {
                                const hint = json.available
                                    ? `  available: ${json.available.join(', ')}`
                                    : '';
                                appendLine(`[ERR] ${json.error}${hint}`, 'uplink-err');
                            }
                        } catch (e) {
                            appendLine(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'uplink-err');
                        }
                        return;
                    }

                    if (cmd === 'uplinktest') {
                        const t0 = Date.now();
                        try {
                            const [healthRes, metaRes] = await Promise.all([
                                fetch(`${serverUrl}/health`),
                                fetch(`${serverUrl}/api/uplink/meta`),
                            ]);
                            const latency = Date.now() - t0;
                            const health = await healthRes.json();
                            const meta = await metaRes.json();
                            appendLine(`[OK] uplink server reachable  latency=${latency}ms  transport=${health.transport}`, 'uplink-ok');
                            appendLine(`     cfs_core params: ${meta.scopes.cfs_core.join(', ')}`, 'uplink-info');
                            appendLine(`     mavlink_bridge params: ${meta.scopes.mavlink_bridge.join(', ')}`, 'uplink-info');
                        } catch (e) {
                            appendLine(`[ERR] uplink server unreachable (${serverUrl}): ${e.message}`, 'uplink-err');
                        }
                        return;
                    }

                    if (cmd === 'recovery') {
                        // payload_hex는 선택적 raw override (uplink_app이 현재 무시함)
                        const [, payloadHex] = parts;
                        const body = {};
                        if (payloadHex) body.payload_hex = payloadHex;
                        try {
                            const res = await fetch(`${serverUrl}/api/uplink/recovery`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(body),
                            });
                            const json = await res.json();
                            if (json.ok) {
                                appendLine(
                                    `[OK] RECOVERY sent  seq=${json.seq}`,
                                    'uplink-ok',
                                );
                            } else {
                                appendLine(`[ERR] ${json.error}`, 'uplink-err');
                            }
                        } catch (e) {
                            appendLine(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'uplink-err');
                        }
                        return;
                    }

                    appendLine(
                        `[ERR] unknown command '${cmd}' — type 'help' for available commands`,
                        'uplink-err',
                    );
                }

                return {
                    show(container) {
                        const wrapper = document.createElement('div');
                        wrapper.className = 'uplink-terminal';

                        outputEl = document.createElement('div');
                        outputEl.className = 'uplink-output';
                        outputEl.setAttribute('aria-label', 'terminal output');
                        outputEl.setAttribute('aria-live', 'polite');

                        const inputRow = document.createElement('div');
                        inputRow.className = 'uplink-input-row';

                        const prompt = document.createElement('span');
                        prompt.className = 'uplink-prompt';
                        prompt.textContent = '>';

                        inputEl = document.createElement('input');
                        inputEl.className = 'uplink-input';
                        inputEl.type = 'text';
                        inputEl.autocomplete = 'off';
                        inputEl.spellcheck = false;
                        inputEl.placeholder = 'type command  (help for usage)';
                        inputEl.setAttribute('aria-label', 'command input');

                        inputEl.addEventListener('keydown', (e) => {
                            if (e.key === 'Enter') {
                                const line = inputEl.value;
                                inputEl.value = '';
                                dispatch(line);
                            }
                        });

                        inputRow.appendChild(prompt);
                        inputRow.appendChild(inputEl);
                        wrapper.appendChild(outputEl);
                        wrapper.appendChild(inputRow);
                        container.appendChild(wrapper);

                        appendLine('cFS Uplink CLI  —  type "help" for available commands', 'uplink-info');
                        inputEl.focus();
                    },

                    destroy() {
                        outputEl = null;
                        inputEl = null;
                    },
                };
            },

            priority() {
                return 1;
            },
        });
    };
}
