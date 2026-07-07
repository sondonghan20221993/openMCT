import './form.css';

const DEFAULT_SERVER = 'http://127.0.0.1:8082';

// 서버 /api/uplink/meta 조회 실패 시 사용하는 폴백 param 목록
// (fc_serial_ws_server.py의 CFS_CORE_PARAMS / MAVLINK_BRIDGE_PARAMS와 동일)
const FALLBACK_PARAMS = {
    cfs_core: [
        'attitude_timeout_ms',
        'local_timeout_ms',
        'gps_timeout_ms',
        'ekf_timeout_ms',
        'bridge_timeout_ms',
        'publish_period_ms',
    ],
    mavlink_bridge: [
        'attitude_interval_us',
        'local_position_interval_us',
        'global_position_interval_us',
        'gps_raw_interval_us',
        'ekf_status_interval_us',
        'reconnect_interval_ms',
        'heartbeat_interval_ms',
    ],
};

const MAX_WAYPOINTS = 16;   // spec §18.4.6.2

export default function uplinkGUIPlugin(serverUrl = DEFAULT_SERVER) {
    return function install(openmct) {
        openmct.types.addType('uplink.gui', {
            name: 'Uplink GUI',
            description: 'Form-based GUI for sending uplink commands to cFS uplink_app',
            cssClass: 'icon-gear',
        });

        openmct.objectViews.addProvider({
            name: 'Uplink GUI',
            key: 'uplink-gui-view',
            cssClass: 'icon-gear',

            canView(domainObject) {
                return domainObject.type === 'uplink.gui';
            },

            view() {
                // scope별 param 목록 (meta 조회로 갱신, 실패 시 폴백)
                let params = { ...FALLBACK_PARAMS };
                let logEl = null;
                let els = {};   // 폼 요소 참조

                function log(text, cls) {
                    if (!logEl) return;
                    const line = document.createElement('div');
                    line.className = 'ug-log-line' + (cls ? ' ' + cls : '');
                    line.textContent = text;
                    logEl.appendChild(line);
                    logEl.scrollTop = logEl.scrollHeight;
                }

                async function postJSON(path, body) {
                    const res = await fetch(`${serverUrl}${path}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    return res.json();
                }

                // --- 상단 상태바: health + meta 자동 조회 (CLI의 uplinktest 대체) ---
                async function refreshStatus() {
                    if (!els.dot) return;   // 뷰가 이미 destroy됨
                    els.dot.className = 'ug-dot';
                    els.statusText.textContent = '연결 확인 중…';
                    const t0 = Date.now();
                    try {
                        const [healthRes, metaRes] = await Promise.all([
                            fetch(`${serverUrl}/health`),
                            fetch(`${serverUrl}/api/uplink/meta`),
                        ]);
                        if (!els.dot) return;   // await 도중 뷰가 닫혔으면 중단
                        const latency = Date.now() - t0;
                        const health = await healthRes.json();
                        const meta = await metaRes.json();
                        if (!els.dot) return;
                        if (meta.scopes) {
                            params = meta.scopes;
                            rebuildParamOptions();
                        }
                        els.dot.className = 'ug-dot ok';
                        els.statusText.textContent =
                            `연결됨   latency=${latency}ms   transport=${health.transport}`;
                    } catch (e) {
                        if (!els.dot) return;   // await 도중 뷰가 닫혔으면 중단
                        els.dot.className = 'ug-dot err';
                        els.statusText.textContent = `서버 응답 없음 (${serverUrl}) — 폴백 param 사용`;
                        params = { ...FALLBACK_PARAMS };
                        rebuildParamOptions();
                    }
                }

                function rebuildParamOptions() {
                    if (!els.scope || !els.param) return;   // 뷰가 이미 destroy됨
                    const scope = els.scope.value;
                    els.param.innerHTML = '';
                    (params[scope] || []).forEach((p) => {
                        const opt = document.createElement('option');
                        opt.value = p;
                        opt.textContent = p;
                        els.param.appendChild(opt);
                    });
                }

                // --- CONFIG 전송 ---
                async function sendConfig() {
                    const scope = els.scope.value;
                    const param = els.param.value;
                    if (!param) { log('[ERR] param을 선택하세요', 'ug-err'); return; }
                    if (els.value.value.trim() === '') { log('[ERR] value를 입력하세요', 'ug-err'); return; }
                    const value = Number(els.value.value);
                    if (!Number.isInteger(value) || value < 0 || value > 0xFFFFFFFF) {
                        log('[ERR] value must be a uint32 integer (0 – 4294967295)', 'ug-err');
                        return;
                    }
                    try {
                        const json = await postJSON('/api/uplink/config', { scope, param, value });
                        if (json.ok) {
                            log(`[OK] CONFIG accepted  seq=${json.seq}  ${json.scope}.${json.param}=${json.value}`, 'ug-ok');
                        } else {
                            const hint = json.available ? `  available: ${json.available.join(', ')}` : '';
                            log(`[ERR] ${json.error}${hint}`, 'ug-err');
                        }
                    } catch (e) {
                        log(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'ug-err');
                    }
                }

                // --- ROUTE 전송 ---
                async function sendRoute() {
                    const routeType = els.routeForm.querySelector('input[name="rtype"]:checked').value;
                    const rows = [...els.wpList.querySelectorAll('.ug-wp')];
                    const waypoints = [];
                    for (const row of rows) {
                        const inputs = [...row.querySelectorAll('input')];
                        if (inputs.some((i) => i.value.trim() === '')) {
                            log('[ERR] 모든 웨이포인트의 x,y,z를 채우세요 (빈칸 불가)', 'ug-err');
                            return;
                        }
                        const [x, y, z] = inputs.map((i) => Number(i.value));
                        if ([x, y, z].some((n) => !Number.isFinite(n))) {
                            log('[ERR] 웨이포인트 x,y,z는 숫자여야 합니다', 'ug-err');
                            return;
                        }
                        waypoints.push([x, y, z]);
                    }
                    if (waypoints.length === 0) { log('[ERR] 웨이포인트가 최소 1개 필요합니다', 'ug-err'); return; }
                    try {
                        const json = await postJSON('/api/uplink/route', { route_type: routeType, waypoints });
                        if (json.ok) {
                            log(`[OK] ROUTE sent  seq=${json.seq}  ${json.route_type}  wps=${json.waypoint_count}`, 'ug-ok');
                        } else {
                            log(`[ERR] ${json.error}`, 'ug-err');
                        }
                    } catch (e) {
                        log(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'ug-err');
                    }
                }

                // --- RECOVERY 전송 ---
                async function sendRecovery() {
                    const hex = els.recoveryHex.value.trim();
                    const body = hex ? { payload_hex: hex } : {};
                    try {
                        const json = await postJSON('/api/uplink/recovery', body);
                        if (json.ok) {
                            log(`[OK] RECOVERY sent  seq=${json.seq}`, 'ug-ok');
                        } else {
                            log(`[ERR] ${json.error}`, 'ug-err');
                        }
                    } catch (e) {
                        log(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'ug-err');
                    }
                }

                function addWaypointRow(x = '', y = '', z = '') {
                    const rows = els.wpList.querySelectorAll('.ug-wp').length;
                    if (rows >= MAX_WAYPOINTS) { log(`[ERR] 최대 ${MAX_WAYPOINTS}개까지`, 'ug-err'); return; }
                    const wp = document.createElement('div');
                    wp.className = 'ug-wp';
                    wp.innerHTML =
                        `<span class="idx"></span>` +
                        `<input class="ug-num" type="number" step="any" placeholder="x" value="${x}">` +
                        `<input class="ug-num" type="number" step="any" placeholder="y" value="${y}">` +
                        `<input class="ug-num" type="number" step="any" placeholder="z" value="${z}">` +
                        `<button class="rm" type="button">✕</button>`;
                    wp.querySelector('.rm').addEventListener('click', () => { wp.remove(); renumber(); });
                    els.wpList.appendChild(wp);
                    renumber();
                }
                function renumber() {
                    els.wpList.querySelectorAll('.ug-wp').forEach((r, i) => {
                        r.querySelector('.idx').textContent = (i + 1) + '.';
                    });
                }

                return {
                    show(container) {
                        const root = document.createElement('div');
                        root.className = 'uplink-gui';
                        root.innerHTML = `
                            <div class="ug-status">
                                <span class="ug-dot" data-el="dot"></span>
                                <span class="ug-status-text" data-el="statusText">연결 확인 중…</span>
                                <button class="ug-refresh" data-el="refresh" type="button">재확인</button>
                            </div>

                            <div class="ug-panel">
                                <h3>Config</h3>
                                <div class="ug-row">
                                    <label>scope</label>
                                    <select data-el="scope">
                                        <option value="cfs_core">cfs_core</option>
                                        <option value="mavlink_bridge">mavlink_bridge</option>
                                    </select>
                                </div>
                                <div class="ug-row">
                                    <label>param</label>
                                    <select data-el="param"></select>
                                </div>
                                <div class="ug-row">
                                    <label>value</label>
                                    <input class="ug-num" type="number" min="0" max="4294967295" step="1" data-el="value" placeholder="0">
                                    <span class="hint">uint32 (0 – 4294967295)</span>
                                </div>
                                <div class="ug-row" style="justify-content:flex-end;margin-bottom:0;">
                                    <button class="ug-send" data-el="sendConfig" type="button">CONFIG 전송</button>
                                </div>
                            </div>

                            <div class="ug-panel" data-el="routeForm">
                                <h3>Route Update</h3>
                                <div class="ug-row">
                                    <label>type</label>
                                    <div class="ug-radios">
                                        <label><input type="radio" name="rtype" value="mission" checked> mission</label>
                                        <label><input type="radio" name="rtype" value="landing"> landing</label>
                                    </div>
                                </div>
                                <div data-el="wpList"></div>
                                <button class="ug-add" data-el="addWp" type="button">+ 웨이포인트 추가</button>
                                <div class="ug-row" style="justify-content:flex-end;margin:8px 0 0;">
                                    <button class="ug-send" data-el="sendRoute" type="button">ROUTE 전송</button>
                                </div>
                            </div>

                            <div class="ug-panel">
                                <h3>Recovery</h3>
                                <div class="ug-row">
                                    <label>hex</label>
                                    <input class="ug-text" type="text" data-el="recoveryHex" placeholder="(선택) payload_hex — 비우면 기본 RECOVERY">
                                    <button class="ug-send" data-el="sendRecovery" type="button">RECOVERY 전송</button>
                                </div>
                            </div>

                            <div class="ug-log" data-el="log"></div>
                        `;

                        // data-el 요소 수집
                        root.querySelectorAll('[data-el]').forEach((el) => { els[el.dataset.el] = el; });
                        logEl = els.log;

                        // 이벤트 연결
                        els.refresh.addEventListener('click', refreshStatus);
                        els.scope.addEventListener('change', rebuildParamOptions);
                        els.sendConfig.addEventListener('click', sendConfig);
                        els.addWp.addEventListener('click', () => addWaypointRow());
                        els.sendRoute.addEventListener('click', sendRoute);
                        els.sendRecovery.addEventListener('click', sendRecovery);

                        rebuildParamOptions();
                        addWaypointRow('0', '-10', '3');   // 예시 웨이포인트 1개
                        container.appendChild(root);

                        log('cFS Uplink GUI (prototype) — 서버 연결을 확인합니다', 'ug-info');
                        refreshStatus();
                    },

                    destroy() {
                        logEl = null;
                        els = {};
                    },
                };
            },

            priority() { return 1; },
        });
    };
}
