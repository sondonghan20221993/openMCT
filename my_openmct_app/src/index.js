import openmct from 'openmct';
import '../node_modules/openmct/dist/darkmatterTheme.css';
import cfsRealtimePlugin from './plugins/cfsRealtime/plugin';

const THIRTY_SECONDS = 30 * 1000;
const FIVE_MINUTES = 5 * 60 * 1000;

openmct.setAssetPath('/node_modules/openmct/dist/');
openmct.install(openmct.plugins.LocalStorage('', 'my_openmct_app'));
openmct.install(openmct.plugins.MyItems());
openmct.install(openmct.plugins.LocalTimeSystem());
openmct.install(openmct.plugins.UTCTimeSystem());
openmct.install(
    openmct.plugins.Conductor({
        menuOptions: [
            {
                name: 'Fixed',
                timeSystem: 'utc',
                bounds: {
                    start: Date.now() - FIVE_MINUTES,
                    end: Date.now()
                }
            },
            {
                name: 'Realtime',
                timeSystem: 'utc',
                clock: 'local',
                clockOffsets: {
                    start: -FIVE_MINUTES,
                    end: THIRTY_SECONDS
                }
            }
        ]
    })
);
openmct.install(openmct.plugins.Clock({ enableClockIndicator: true }));

openmct.install(cfsRealtimePlugin());

openmct.on('start', () => {
    openmct.time.setTimeSystem('utc');
    openmct.time.setMode('realtime', {
        start: -FIVE_MINUTES,
        end: THIRTY_SECONDS
    });
});

openmct.start(document.body);
