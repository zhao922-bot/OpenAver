import { stateConfig }       from '@/settings/state-config.js';
import { stateProviders }    from '@/settings/state-providers.js';
import { stateUI }           from '@/settings/state-ui.js';

function mergeState(...parts) {
    const target = {};
    for (const part of parts) {
        Object.defineProperties(target, Object.getOwnPropertyDescriptors(part));
    }
    return target;
}

document.addEventListener('alpine:init', () => {
    Alpine.data('settings', () => mergeState(
        stateConfig(),
        stateProviders(),
        stateUI(),
    ));
});
