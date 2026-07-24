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
    lora_tdm: [
        'downlink_protocol',
    ],
};

// 서버 /api/uplink/meta의 bounds 조회 실패 시 폴백 (fc_serial_ws_server.py의
// PARAM_BOUNDS와 동일 — cFS 기체측 min/max 값 기준)
const FALLBACK_BOUNDS = {
    cfs_core: {
        attitude_timeout_ms: [100, 60000],
        local_timeout_ms:    [100, 60000],
        gps_timeout_ms:      [100, 60000],
        ekf_timeout_ms:      [100, 60000],
        bridge_timeout_ms:   [100, 60000],
        publish_period_ms:   [100, 60000],
    },
    mavlink_bridge: {
        attitude_interval_us:        [10000, 10000000],
        local_position_interval_us:  [10000, 10000000],
        global_position_interval_us: [10000, 10000000],
        gps_raw_interval_us:         [10000, 10000000],
        ekf_status_interval_us:      [10000, 10000000],
        reconnect_interval_ms:       [100, 60000],
        heartbeat_interval_ms:       [100, 60000],
    },
    lora_tdm: {
        downlink_protocol: [0, 1],
    },
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
                // scope.param -> [min, max] (meta 조회로 갱신, 실패 시 폴백)
                let bounds = { ...FALLBACK_BOUNDS };
                let logEl = null;
                let els = {};   // 폼 요소 참조

                // Downlink UFB (Uplink Feedback) monitoring
                let downlinkSocket = null;
                let pendingCommand = null;  // { kind, seq, timestamp, retryCount, describe, resend }
                let ufbTimeoutHandle = null;

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

                // --- Downlink UFB monitoring (WebSocket) ---
                function connectDownlinkSocket() {
                    if (downlinkSocket) return;
                    downlinkSocket = new WebSocket('ws://127.0.0.1:8765');
                    downlinkSocket.onmessage = (event) => {
                        try {
                            const msg = JSON.parse(event.data);
                            if (msg.uplink_fb !== undefined) {
                                onUFBReceived(msg.uplink_fb);
                            }
                        } catch { }
                    };
                    downlinkSocket.onclose = () => {
                        downlinkSocket = null;
                        setTimeout(connectDownlinkSocket, 1000);
                    };
                    downlinkSocket.onerror = () => {
                        downlinkSocket?.close();
                    };
                }

                function armPendingCommand(kind, seq, describe, resend) {
                    clearPendingCommand();
                    pendingCommand = { kind, seq, timestamp: Date.now(), retryCount: 0, describe, resend };
                    ufbTimeoutHandle = setTimeout(() => {
                        if (pendingCommand) {
                            log(`[⏱️ Timeout] 기체 응답 없음 (>1s) — ${pendingCommand.describe()}`, 'ug-err');
                            pendingCommand = null;
                        }
                    }, 1000);
                }

                function clearPendingCommand() {
                    if (ufbTimeoutHandle) {
                        clearTimeout(ufbTimeoutHandle);
                        ufbTimeoutHandle = null;
                    }
                    pendingCommand = null;
                }

                function onUFBReceived(ufb) {
                    if (!pendingCommand) return;
                    if (ufbTimeoutHandle) clearTimeout(ufbTimeoutHandle);

                    if (ufb === 0) {
                        // UFB_OK는 "정상 처리"와 "보고할 pending 결과 없음(default)"을
                        // 구분하지 못하는 구조적 한계가 있음(lora_tdm_app_behavior_spec.md
                        // §10 "알려진 한계" 참조) — CRC/SEQ_FAIL/STATE_BLOCKED가 아니라는
                        // 것만 확정, "적용됨" 단정은 하지 않는다.
                        log(`[✅ UFB=0] 오류 없이 수신됨 (CRC/SEQ/STATE 정상) — ${pendingCommand.describe()}`, 'ug-ok');
                        clearPendingCommand();
                    } else if (ufb === 1) {
                        pendingCommand.retryCount++;
                        if (pendingCommand.retryCount <= 3) {
                            log(`[❌ UFB=1] CRC 오류! 같은 seq=${pendingCommand.seq}로 재전송합니다... (${pendingCommand.retryCount}/3)`, 'ug-err');
                            pendingCommand.timestamp = Date.now();
                            ufbTimeoutHandle = setTimeout(() => {
                                if (pendingCommand) {
                                    log(`[⏱️ Timeout] 재전송 응답 없음 (>1s)`, 'ug-err');
                                    pendingCommand = null;
                                }
                            }, 1000);
                            pendingCommand.resend();
                        } else {
                            log(`[❌] CRC 재전송 3회 실패 — ${pendingCommand.describe()}`, 'ug-err');
                            clearPendingCommand();
                        }
                    } else if (ufb === 2) {
                        log(`[⚠️ UFB=2] 시퀀스 오류, 수동으로 다시 시도하세요 — ${pendingCommand.describe()}`, 'ug-warn');
                        clearPendingCommand();
                    } else if (ufb === 3) {
                        log(`[🚫 UFB=3] 헬스 게이트에 막힘 (health_state 확인 필요) — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 4) {
                        log(`[❌ UFB=4] 일반 처리 실패 — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 5) {
                        log(`[❌ UFB=5] 프로토콜 버전 불일치 — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 6) {
                        log(`[❌ UFB=6] 알 수 없는 커맨드 클래스 — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 7) {
                        log(`[❌ UFB=7] 페이로드 길이 불일치 — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 8) {
                        log(`[❌ UFB=8] 라우팅 대상 없음 — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 9) {
                        log(`[❌ UFB=9] 라우트 갱신 거부 — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 10) {
                        log(`[❌ UFB=0x0A] 프록시 명령 체크섬 불일치 — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 11) {
                        log(`[❌ UFB=0x0B] VIEWPOINT 페이로드 거부 — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 12) {
                        // cfs-telemetry-app BL-CTR(2026-07-22): counter management
                        // 명령의 scope/action 오류 또는 Level 3 인가 차단
                        log(`[❌ UFB=0x0C] counter management 거부 (scope/action 오류 또는 인가 차단) — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    } else if (ufb === 13) {
                        // cfs-telemetry-app BL-44(2026-07-24, §18.4.6.8): flight mode
                        // payload(flight_mode/waypoint_start_index) 오류
                        log(`[❌ UFB=0x0D] flight mode 거부 (mode/waypoint_index 오류) — ${pendingCommand.describe()}`, 'ug-err');
                        clearPendingCommand();
                    }
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
                            if (meta.bounds) bounds = meta.bounds;
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
                        bounds = { ...FALLBACK_BOUNDS };
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
                    updateValueBounds();
                }

                // 선택된 scope.param의 min/max를 value input에 반영 (항목별 제한 상이)
                function updateValueBounds() {
                    if (!els.scope || !els.param || !els.value) return;
                    const scope = els.scope.value;
                    const param = els.param.value;
                    const range = bounds[scope] && bounds[scope][param];
                    if (range) {
                        const [lo, hi] = range;
                        els.value.min = String(lo);
                        els.value.max = String(hi);
                        els.value.placeholder = `${lo} – ${hi}`;
                        if (els.valueHint) els.valueHint.textContent = `허용 범위: ${lo} – ${hi}`;
                    } else {
                        els.value.min = '0';
                        els.value.max = '4294967295';
                        els.value.placeholder = '0';
                        if (els.valueHint) els.valueHint.textContent = '';
                    }
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
                    const range = bounds[scope] && bounds[scope][param];
                    if (range && (value < range[0] || value > range[1])) {
                        log(`[ERR] ${param}은(는) ${range[0]} – ${range[1]} 범위여야 합니다`, 'ug-err');
                        return;
                    }
                    const force = !!(els.force && els.force.checked);
                    try {
                        const json = await postJSON('/api/uplink/config', { scope, param, value, force });
                        if (json.ok) {
                            log(`[OK] CONFIG accepted  seq=${json.seq}  ${json.scope}.${json.param}=${json.value}`, 'ug-ok');
                            const describe = () => `config ${scope}.${param}=${value}`;
                            // BL-24: 새 seq 재조립이 아니라 같은 seq 재큐잉(진짜 재전송) —
                            // 원본이 이미 수락됐어도 기체가 DUPLICATE로 무시해 이중 실행 방지
                            const resend = () => postJSON('/api/uplink/resend', { seq: json.seq }).catch(() => { });
                            armPendingCommand('config', json.seq, describe, resend);
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
                            const describe = () => `route ${routeType} (${waypoints.length} waypoints)`;
                            // BL-24: 같은 seq 재큐잉(진짜 재전송)
                            const resend = () => postJSON('/api/uplink/resend', { seq: json.seq }).catch(() => { });
                            armPendingCommand('route', json.seq, describe, resend);
                        } else {
                            log(`[ERR] ${json.error}`, 'ug-err');
                        }
                    } catch (e) {
                        log(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'ug-err');
                    }
                }

                // --- RECOVERY 전송 ---
                const RECOVERY_ACTION_NAMES = ['RESET_COUNTER', 'RESTART_BRIDGE', 'PARSER_RESET',
                                               'SERIAL_RECONNECT', 'RESTART_UPLINK', 'RESTART_LORA'];

                async function sendRecovery() {
                    // ⑦(2026-07-22): raw hex가 있으면 그대로(고급), 없으면 드롭다운
                    // action으로 payload byte[0]을 구성 — 나머지 바이트는 0(로그용 필드)
                    const hex = els.recoveryHex.value.trim();
                    const action = Number(els.recoveryAction.value);
                    const payloadHex = hex || action.toString(16).padStart(2, '0');
                    const label = hex ? `raw(${hex})` : RECOVERY_ACTION_NAMES[action];
                    try {
                        const json = await postJSON('/api/uplink/recovery', { payload_hex: payloadHex });
                        if (json.ok) {
                            log(`[OK] RECOVERY ${label} sent  seq=${json.seq}`, 'ug-ok');
                            const describe = () => `recovery ${label}`;
                            // BL-24: 같은 seq 재큐잉(진짜 재전송) — sendRecovery() 재호출이면
                            // 새 seq+새 request_token으로 별개 명령이 돼 이중 실행 위험
                            const resend = () => postJSON('/api/uplink/resend', { seq: json.seq }).catch(() => { });
                            armPendingCommand('recovery', json.seq, describe, resend);
                        } else {
                            log(`[ERR] ${json.error}`, 'ug-err');
                        }
                    } catch (e) {
                        log(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'ug-err');
                    }
                }

                // --- COUNTER 전송 (⑦, class 7 §18.4.6.7) ---
                async function sendCounter() {
                    const scope = els.counterScope.value;
                    try {
                        const json = await postJSON('/api/uplink/counter', { scope });
                        if (json.ok) {
                            log(`[OK] COUNTER reset sent  seq=${json.seq}  scope=${json.scope}`, 'ug-ok');
                            const describe = () => `counter reset ${scope}`;
                            const resend = () => postJSON('/api/uplink/resend', { seq: json.seq }).catch(() => { });
                            armPendingCommand('counter', json.seq, describe, resend);
                        } else {
                            const hint = json.available ? `  available: ${json.available.join(', ')}` : '';
                            log(`[ERR] ${json.error}${hint}`, 'ug-err');
                        }
                    } catch (e) {
                        log(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'ug-err');
                    }
                }

                // --- FLIGHT_MODE 전송 (BL-44, class 8, §18.4.6.8) ---
                async function sendFlightMode() {
                    const mode = els.flightMode.value;
                    const waypointIndex = mode === 'waypoint' ? Number(els.flightModeWpIndex.value || 0) : 0;
                    const force = !!(els.flightModeForce && els.flightModeForce.checked);
                    try {
                        const json = await postJSON('/api/uplink/flight_mode',
                            { mode, waypoint_start_index: waypointIndex, force });
                        if (json.ok) {
                            log(`[OK] FLIGHT_MODE ${mode} sent  seq=${json.seq}` +
                                (mode === 'waypoint' ? `  wp_idx=${waypointIndex}` : '') +
                                (force ? '  [FORCE]' : ''), 'ug-ok');
                            const describe = () => `flight_mode ${mode}`;
                            const resend = () => postJSON('/api/uplink/resend', { seq: json.seq }).catch(() => { });
                            armPendingCommand('flight_mode', json.seq, describe, resend);
                        } else {
                            log(`[ERR] ${json.error}`, 'ug-err');
                        }
                    } catch (e) {
                        log(`[ERR] server unreachable (${serverUrl}): ${e.message}`, 'ug-err');
                    }
                }

                // --- DIAGNOSTIC 전송 (class 6) — waypoint readback 트리거 포함 ---
                const DIAG_ACTIONS_BY_TARGET = {
                    lora_tdm: ['link_status', 'rx_stats', 'tx_stats'],
                    cfs_core: ['route_readback'],
                };

                function rebuildDiagActionOptions() {
                    const target = els.diagTarget.value;
                    const actions = DIAG_ACTIONS_BY_TARGET[target] || [];
                    els.diagAction.innerHTML = actions.map((a) => `<option value="${a}">${a}</option>`).join('');
                }

                // route_readback(cfs_core, 0x1913 왕복)만 완료까지 폴링해 결과 표시.
                // spec §4.3 "미완: ground 측 GUI 패널" — RouteReadbackAssembler(서버)는
                // 이미 파싱/재조립하지만 콘솔 로그만 남기던 것을 GET으로 노출해 여기서 표시.
                async function pollRouteReadback() {
                    const started = Date.now();
                    const timeoutMs = 5000;
                    while (Date.now() - started < timeoutMs) {
                        await new Promise((r) => setTimeout(r, 500));
                        try {
                            const st = await (await fetch(`${serverUrl}/api/uplink/route_readback`)).json();
                            if (st.status === 'pending') {
                                log(`[…] route readback 진행 중  ${st.progress}`, 'ug-info');
                            } else if (st.status === 'complete') {
                                log(`[✅] route readback 완료  route_type=${st.route_type}  ` +
                                    `waypoints=${JSON.stringify(st.waypoints)}`, 'ug-ok');
                                return;
                            }
                        } catch (e) {
                            break;
                        }
                    }
                    log(`[⚠️] route readback 응답 시간초과(>5s) — 링크 확인 필요`, 'ug-warn');
                }

                async function sendDiagnostic() {
                    const target = els.diagTarget.value;
                    const action = els.diagAction.value;
                    try {
                        const json = await postJSON('/api/uplink/diagnostic', { target, action });
                        if (json.ok) {
                            log(`[OK] DIAGNOSTIC ${target}/${action} sent  seq=${json.seq}`, 'ug-ok');
                            const describe = () => `diagnostic ${target}/${action}`;
                            const resend = () => postJSON('/api/uplink/resend', { seq: json.seq }).catch(() => { });
                            armPendingCommand('diagnostic', json.seq, describe, resend);
                            if (target === 'cfs_core' && action === 'route_readback') {
                                pollRouteReadback();
                            }
                        } else {
                            const hint = json.available ? `  available: ${json.available.join(', ')}` : '';
                            log(`[ERR] ${json.error}${hint}`, 'ug-err');
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
                                    <!-- ⑦(2026-07-22): cfs_core scope는 GUI에서 제외 — 전부 헬스판정
                                         튜닝 노브(timeout/period)라 실운용에서 바꿀 일이 없고, 비행 중
                                         변경은 오히려 위험(헬스 강등 기준이 느슨해짐). 기체 CONFIG
                                         scope=1 처리와 서버 /api/uplink/meta는 그대로 유지 — 벤치
                                         튜닝은 CLI/HTTP로 여전히 가능. UPLINK_GUI_ENHANCEMENTS.md ⑦ 참조 -->
                                    <select data-el="scope">
                                        <option value="mavlink_bridge">mavlink_bridge</option>
                                        <option value="lora_tdm">lora_tdm</option>
                                    </select>
                                </div>
                                <div class="ug-row">
                                    <label>param</label>
                                    <select data-el="param"></select>
                                </div>
                                <div class="ug-row">
                                    <label>value</label>
                                    <input class="ug-num" type="number" min="0" max="4294967295" step="1" data-el="value" placeholder="0">
                                    <span class="hint" data-el="valueHint">uint32 (0 – 4294967295)</span>
                                </div>
                                <div class="ug-row">
                                    <label title="벤치 테스트 전용 — DEGRADED/FAILED에서도 이 명령 하나만 health gate 우회 (§18.10.2)">
                                        <input type="checkbox" data-el="force"> ⚠️ force (bench-only, health gate 우회)
                                    </label>
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
                                    <label>action</label>
                                    <select class="ug-select" data-el="recoveryAction">
                                        <option value="0">RESET_COUNTER (cfs_core 카운터 리셋)</option>
                                        <option value="1">RESTART_BRIDGE (mavlink_bridge 재시작)</option>
                                        <option value="2">PARSER_RESET (파서만 리셋)</option>
                                        <option value="3">SERIAL_RECONNECT (시리얼만 재연결)</option>
                                        <option value="4">RESTART_UPLINK (uplink 재시작)</option>
                                        <option value="5">RESTART_LORA (lora_tdm 재시작)</option>
                                    </select>
                                </div>
                                <div class="ug-row">
                                    <label>hex</label>
                                    <input class="ug-text" type="text" data-el="recoveryHex" placeholder="(고급) raw payload_hex — 입력 시 드롭다운보다 우선">
                                    <button class="ug-send" data-el="sendRecovery" type="button">RECOVERY 전송</button>
                                </div>
                            </div>

                            <div class="ug-panel">
                                <h3>Counter Reset <span style="font-weight:normal;opacity:.7">(class 7, §18.4.6.7)</span></h3>
                                <div class="ug-row">
                                    <label>scope</label>
                                    <select class="ug-select" data-el="counterScope">
                                        <option value="mavlink_bridge">mavlink_bridge</option>
                                        <option value="cfs_core">cfs_core</option>
                                        <option value="uplink">uplink</option>
                                        <option value="lora_tdm">lora_tdm</option>
                                    </select>
                                    <button class="ug-send" data-el="sendCounter" type="button">COUNTER 전송</button>
                                </div>
                            </div>

                            <div class="ug-panel">
                                <h3>Flight Mode <span style="font-weight:normal;opacity:.7">(class 8, §18.4.6.8, BL-44)</span></h3>
                                <div class="ug-row">
                                    <label>mode</label>
                                    <select class="ug-select" data-el="flightMode">
                                        <option value="hover">HOVER (제자리 대기 — health gate 예외)</option>
                                        <option value="waypoint">WAYPOINT (경로 비행 — health gate 정상 적용)</option>
                                        <option value="land">LAND (착륙 — health gate 예외)</option>
                                    </select>
                                </div>
                                <div class="ug-row">
                                    <label>wp index</label>
                                    <input class="ug-num" type="number" min="0" max="255" step="1" data-el="flightModeWpIndex" placeholder="0" value="0">
                                    <span class="hint">WAYPOINT 전용 — MISSION_SET_CURRENT 대상 인덱스</span>
                                </div>
                                <div class="ug-row">
                                    <label><input type="checkbox" data-el="flightModeForce"> ⚠️ force (bench-only, health gate 우회 — WAYPOINT에만 영향)</label>
                                    <button class="ug-send" data-el="sendFlightMode" type="button">FLIGHT MODE 전송</button>
                                </div>
                            </div>

                            <div class="ug-panel">
                                <h3>Diagnostic <span style="font-weight:normal;opacity:.7">(class 6)</span></h3>
                                <div class="ug-row">
                                    <label>target</label>
                                    <select class="ug-select" data-el="diagTarget">
                                        <option value="lora_tdm">lora_tdm</option>
                                        <option value="cfs_core">cfs_core</option>
                                    </select>
                                    <label>action</label>
                                    <select class="ug-select" data-el="diagAction"></select>
                                    <button class="ug-send" data-el="sendDiagnostic" type="button">DIAGNOSTIC 전송</button>
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
                        els.param.addEventListener('change', updateValueBounds);
                        els.sendConfig.addEventListener('click', sendConfig);
                        els.addWp.addEventListener('click', () => addWaypointRow());
                        els.sendRoute.addEventListener('click', sendRoute);
                        els.sendRecovery.addEventListener('click', sendRecovery);
                        els.sendCounter.addEventListener('click', sendCounter);
                        els.sendFlightMode.addEventListener('click', sendFlightMode);
                        els.diagTarget.addEventListener('change', rebuildDiagActionOptions);
                        els.sendDiagnostic.addEventListener('click', sendDiagnostic);

                        rebuildDiagActionOptions();
                        rebuildParamOptions();
                        addWaypointRow('0', '-10', '3');   // 예시 웨이포인트 1개
                        container.appendChild(root);

                        log('cFS Uplink GUI (prototype) — 서버 연결을 확인합니다', 'ug-info');
                        refreshStatus();
                        connectDownlinkSocket();
                    },

                    destroy() {
                        if (downlinkSocket) {
                            downlinkSocket.close();
                            downlinkSocket = null;
                        }
                        clearPendingCommand();
                        logEl = null;
                        els = {};
                    },
                };
            },

            priority() { return 1; },
        });
    };
}
