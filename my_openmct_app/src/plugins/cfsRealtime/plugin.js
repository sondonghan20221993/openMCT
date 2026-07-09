export default function cfsRealtimePlugin() {
    return function install(openmct) {
        const ROOT = {
            identifier: {
                namespace: 'cfs',
                key: 'root'
            },
            name: 'cFS FC Telemetry',
            type: 'folder',
            location: 'ROOT'
        };

        const FOLDERS = {
            attitude: {
                identifier: { namespace: 'cfs', key: 'attitude-folder' },
                name: 'Attitude',
                type: 'folder',
                location: 'cfs:root'
            },
            position: {
                identifier: { namespace: 'cfs', key: 'position-folder' },
                name: 'Position',
                type: 'folder',
                location: 'cfs:root'
            },
            gps: {
                identifier: { namespace: 'cfs', key: 'gps-folder' },
                name: 'GPS',
                type: 'folder',
                location: 'cfs:root'
            },
            status: {
                identifier: { namespace: 'cfs', key: 'status-folder' },
                name: 'Status',
                type: 'folder',
                location: 'cfs:root'
            }
        };

        function makeTelemetryPoint(key, name, location, valueFormat = 'float', unit = '') {
            return {
                identifier: { namespace: 'cfs', key },
                name,
                type: 'telemetry.point',
                telemetry: {
                    values: [
                        {
                            key: 'utc',
                            name: 'Time',
                            format: 'utc',
                            hints: {
                                domain: 1
                            }
                        },
                        {
                            key: 'value',
                            name: 'Value',
                            format: valueFormat,
                            units: unit,
                            hints: {
                                range: 1
                            }
                        }
                    ]
                },
                location
            };
        }

        const OBJECTS = {
            root: ROOT,
            'attitude-folder': FOLDERS.attitude,
            'position-folder': FOLDERS.position,
            'gps-folder': FOLDERS.gps,
            'status-folder': FOLDERS.status,
            'uplink-cli': {
                identifier: { namespace: 'cfs', key: 'uplink-cli' },
                name: 'Uplink CLI',
                type: 'uplink.terminal',
                location: 'cfs:root'
            },
            'uplink-gui': {
                identifier: { namespace: 'cfs', key: 'uplink-gui' },
                name: 'Uplink GUI',
                type: 'uplink.gui',
                location: 'cfs:root'
            },

            roll: makeTelemetryPoint('roll', 'Roll', 'cfs:attitude-folder', 'float', 'rad'),
            pitch: makeTelemetryPoint('pitch', 'Pitch', 'cfs:attitude-folder', 'float', 'rad'),
            yaw: makeTelemetryPoint('yaw', 'Yaw', 'cfs:attitude-folder', 'float', 'rad'),

            rollspeed: makeTelemetryPoint('rollspeed', 'Roll Rate', 'cfs:attitude-folder', 'float', 'rad/s'),
            pitchspeed: makeTelemetryPoint('pitchspeed', 'Pitch Rate', 'cfs:attitude-folder', 'float', 'rad/s'),
            yawspeed: makeTelemetryPoint('yawspeed', 'Yaw Rate', 'cfs:attitude-folder', 'float', 'rad/s'),

            x: makeTelemetryPoint('x', 'X', 'cfs:position-folder', 'float', 'm'),
            y: makeTelemetryPoint('y', 'Y', 'cfs:position-folder', 'float', 'm'),
            z: makeTelemetryPoint('z', 'Z', 'cfs:position-folder', 'float', 'm'),

            vx: makeTelemetryPoint('vx', 'VX', 'cfs:position-folder', 'float', 'm/s'),
            vy: makeTelemetryPoint('vy', 'VY', 'cfs:position-folder', 'float', 'm/s'),
            vz: makeTelemetryPoint('vz', 'VZ', 'cfs:position-folder', 'float', 'm/s'),

            lat: makeTelemetryPoint('lat', 'Latitude', 'cfs:gps-folder', 'float', 'deg'),
            lon: makeTelemetryPoint('lon', 'Longitude', 'cfs:gps-folder', 'float', 'deg'),
            alt: makeTelemetryPoint('alt', 'Altitude', 'cfs:gps-folder', 'float', 'm'),
            sats: makeTelemetryPoint('sats', 'Satellites', 'cfs:gps-folder', 'integer', ''),
            fix: makeTelemetryPoint('fix', 'GPS Fix', 'cfs:gps-folder', 'integer', ''),

            seq: makeTelemetryPoint('seq', 'Sequence', 'cfs:status-folder', 'integer', ''),
            boot_ms: makeTelemetryPoint('boot_ms', 'Boot Time', 'cfs:status-folder', 'integer', 'ms'),
            flags: makeTelemetryPoint('flags', 'EKF Flags', 'cfs:status-folder', 'integer', ''),

            packet_loss: makeTelemetryPoint('packet_loss', 'Packet Loss', 'cfs:status-folder', 'float', '%'),
            heartbeat: makeTelemetryPoint('heartbeat', 'Heartbeat', 'cfs:status-folder', 'integer', ''),
            health_state: makeTelemetryPoint('health_state', 'Health State', 'cfs:status-folder', 'integer', ''),
            fault_code: makeTelemetryPoint('fault_code', 'Fault Code', 'cfs:status-folder', 'integer', '')
        };

        const COMPOSITION = {
            root: [
                FOLDERS.attitude.identifier,
                FOLDERS.position.identifier,
                FOLDERS.gps.identifier,
                FOLDERS.status.identifier,
                { namespace: 'cfs', key: 'uplink-cli' },
                { namespace: 'cfs', key: 'uplink-gui' }
            ],
            'attitude-folder': [
                OBJECTS.roll.identifier,
                OBJECTS.pitch.identifier,
                OBJECTS.yaw.identifier,
                OBJECTS.rollspeed.identifier,
                OBJECTS.pitchspeed.identifier,
                OBJECTS.yawspeed.identifier
            ],
            'position-folder': [
                OBJECTS.x.identifier,
                OBJECTS.y.identifier,
                OBJECTS.z.identifier,
                OBJECTS.vx.identifier,
                OBJECTS.vy.identifier,
                OBJECTS.vz.identifier
            ],
            'gps-folder': [
                OBJECTS.lat.identifier,
                OBJECTS.lon.identifier,
                OBJECTS.alt.identifier,
                OBJECTS.sats.identifier,
                OBJECTS.fix.identifier
            ],
            'status-folder': [
                OBJECTS.seq.identifier,
                OBJECTS.boot_ms.identifier,
                OBJECTS.flags.identifier,
                OBJECTS.packet_loss.identifier,
                OBJECTS.heartbeat.identifier,
                OBJECTS.health_state.identifier,
                OBJECTS.fault_code.identifier
            ]
        };

        const latest = {};
        const subscribers = {};
        let socket = null;
        let db = null;

        // IndexedDB 초기화
        function initIndexedDB() {
            return new Promise((resolve, reject) => {
                const request = indexedDB.open('cfsRealtime', 1);

                request.onerror = () => {
                    console.error('[cfsRealtime] IndexedDB open failed', request.error);
                    reject(request.error);
                };

                request.onsuccess = () => {
                    db = request.result;
                    console.log('[cfsRealtime] IndexedDB initialized');
                    resolve(db);
                };

                request.onupgradeneeded = (event) => {
                    const newDb = event.target.result;
                    if (!newDb.objectStoreNames.contains('telemetry')) {
                        newDb.createObjectStore('telemetry', { keyPath: 'timestamp' });
                        console.log('[cfsRealtime] telemetry store created');
                    }
                };
            });
        }

        // IndexedDB에 저장
        function saveTelemetryToIndexedDB(timestamp, data) {
            if (!db) {
                return;
            }

            try {
                const transaction = db.transaction(['telemetry'], 'readwrite');
                const store = transaction.objectStore('telemetry');
                store.add({ timestamp, data });
            } catch (e) {
                console.error('[cfsRealtime] IndexedDB save error', e);
            }
        }

        // IndexedDB에서 데이터 조회 (CSV 내보내기용)
        function getAllTelemetryFromIndexedDB() {
            return new Promise((resolve, reject) => {
                if (!db) {
                    reject(new Error('IndexedDB not initialized'));
                    return;
                }

                const transaction = db.transaction(['telemetry'], 'readonly');
                const store = transaction.objectStore('telemetry');
                const request = store.getAll();

                request.onsuccess = () => {
                    resolve(request.result);
                };

                request.onerror = () => {
                    reject(request.error);
                };
            });
        }

        // IndexedDB에서 데이터 삭제 (타임스탬프 범위)
        function clearOldTelemetry(olderThanMs) {
            if (!db) {
                return;
            }

            try {
                const transaction = db.transaction(['telemetry'], 'readwrite');
                const store = transaction.objectStore('telemetry');
                const range = IDBKeyRange.upperBound(olderThanMs);
                store.delete(range);
                console.log('[cfsRealtime] Cleared telemetry older than', olderThanMs);
            } catch (e) {
                console.error('[cfsRealtime] IndexedDB clear error', e);
            }
        }

        function connectWebSocket() {
            if (socket) {
                return;
            }

            socket = new WebSocket('ws://127.0.0.1:8765');

            socket.onopen = () => {
                console.log('[cfsRealtime] WebSocket connected');
            };

            socket.onmessage = (event) => {
                let msg;

                try {
                    msg = JSON.parse(event.data);
                } catch (e) {
                    console.warn('[cfsRealtime] bad JSON', event.data);
                    return;
                }

                console.log('[cfsRealtime] message', msg);

                const timestamp = msg.timestamp || Date.now();
                const telemetryData = {};

                Object.keys(OBJECTS).forEach((key) => {
                    if (key === 'root') {
                        return;
                    }

                    if (msg[key] === undefined || msg[key] === null) {
                        return;
                    }

                    const datum = {
                        utc: timestamp,
                        value: Number(msg[key])
                    };

                    console.log('[cfsRealtime] datum', key, datum);

                    latest[key] = datum;
                    telemetryData[key] = msg[key];

                    if (subscribers[key]) {
                        subscribers[key].forEach((callback) => callback(datum));
                    }
                });

                // IndexedDB에 저장
                if (Object.keys(telemetryData).length > 0) {
                    saveTelemetryToIndexedDB(timestamp, telemetryData);
                }
            };

            socket.onerror = (error) => {
                console.error('[cfsRealtime] WebSocket error', error);
            };

            socket.onclose = () => {
                console.warn('[cfsRealtime] WebSocket closed, retrying...');
                socket = null;
                setTimeout(connectWebSocket, 1000);
            };
        }

        openmct.objects.addRoot(ROOT.identifier);

        openmct.objects.addProvider('cfs', {
            get: async function (identifier) {
                return OBJECTS[identifier.key];
            }
        });

        openmct.composition.addProvider({
            appliesTo(domainObject) {
                return domainObject.identifier?.namespace === 'cfs';
            },
            load(domainObject) {
                return Promise.resolve(COMPOSITION[domainObject.identifier.key] || []);
            }
        });

        openmct.telemetry.addProvider({
            supportsRequest(domainObject) {
                return domainObject.identifier?.namespace === 'cfs' &&
                    domainObject.type === 'telemetry.point';
            },

            request(domainObject) {
                const key = domainObject.identifier.key;

                if (latest[key]) {
                    return Promise.resolve([latest[key]]);
                }

                return Promise.resolve([]);
            },

            supportsSubscribe(domainObject) {
                return domainObject.identifier?.namespace === 'cfs' &&
                    domainObject.type === 'telemetry.point';
            },

            subscribe(domainObject, callback) {
                const key = domainObject.identifier.key;

                if (!subscribers[key]) {
                    subscribers[key] = [];
                }

                subscribers[key].push(callback);
                connectWebSocket();

                return function unsubscribe() {
                    subscribers[key] = subscribers[key].filter((cb) => cb !== callback);
                };
            }
        });

        // IndexedDB 초기화 및 WebSocket 연결
        initIndexedDB()
            .then(() => {
                console.log('[cfsRealtime] Ready to save telemetry to IndexedDB');
                connectWebSocket();
            })
            .catch((error) => {
                console.error('[cfsRealtime] Failed to initialize IndexedDB, continuing without storage', error);
                connectWebSocket();
            });
    };
}
