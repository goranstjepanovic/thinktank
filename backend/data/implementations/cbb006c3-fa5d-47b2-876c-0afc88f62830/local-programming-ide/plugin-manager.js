class PluginManager {
    constructor() {
        this.plugins = new Map();
    }

    loadPlugin(pluginName, pluginPath) {
        try {
            const pluginModule = require(pluginPath);
            if (typeof pluginModule.activate === 'function') {
                this.plugins.set(pluginName, { module: pluginModule, active: false });
                console.log(`Loaded plugin: ${pluginName}`);
            } else {
                throw new Error('Plugin does not have an activate function');
            }
        } catch (error) {
            console.error(`Failed to load plugin ${pluginName}:`, error.message);
        }
    }

    activatePlugin(pluginName) {
        const plugin = this.plugins.get(pluginName);
        if (!plugin) {
            console.warn(`Plugin ${pluginName} not found`);
            return;
        }

        try {
            if (typeof plugin.module.activate === 'function') {
                plugin.module.activate();
                plugin.active = true;
                console.log(`Activated plugin: ${pluginName}`);
            } else {
                throw new Error('Activate function is missing in the plugin');
            }
        } catch (error) {
            console.error(`Failed to activate plugin ${pluginName}:`, error.message);
        }
    }

    deactivatePlugin(pluginName) {
        const plugin = this.plugins.get(pluginName);
        if (!plugin || !plugin.active) {
            console.warn(`Plugin ${pluginName} is not active or not found`);
            return;
        }

        try {
            if (typeof plugin.module.deactivate === 'function') {
                plugin.module.deactivate();
                plugin.active = false;
                console.log(`Deactivated plugin: ${pluginName}`);
            } else {
                throw new Error('Deactivate function is missing in the plugin');
            }
        } catch (error) {
            console.error(`Failed to deactivate plugin ${pluginName}:`, error.message);
        }
    }

    unloadPlugin(pluginName) {
        const plugin = this.plugins.get(pluginName);
        if (!plugin) {
            console.warn(`Plugin ${pluginName} not found`);
            return;
        }

        try {
            if (typeof plugin.module.deactivate === 'function') {
                plugin.module.deactivate();
            }
            this.plugins.delete(pluginName);
            console.log(`Unloaded plugin: ${pluginName}`);
        } catch (error) {
            console.error(`Failed to unload plugin ${pluginName}:`, error.message);
        }
    }

    listPlugins() {
        return Array.from(this.plugins.keys()).map(name => ({
            name,
            active: this.plugins.get(name).active
        }));
    }
}

module.exports = PluginManager;