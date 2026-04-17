// renderer.js

const { ipcRenderer } = require('electron');
const fs = require('fs');
const path = require('path');

class PluginManager {
  constructor() {
    this.plugins = [];
  }

  loadPlugins(pluginDirectory) {
    const pluginFiles = fs.readdirSync(pluginDirectory);
    pluginFiles.forEach(file => {
      if (file.endsWith('.js')) {
        try {
          const pluginPath = path.join(pluginDirectory, file);
          const PluginClass = require(pluginPath).default;
          const pluginInstance = new PluginClass();
          this.plugins.push(pluginInstance);
          console.log(`Loaded plugin: ${file}`);
        } catch (error) {
          console.error(`Failed to load plugin: ${file}`, error);
        }
      }
    });
  }

  activatePlugin(pluginName, args) {
    const plugin = this.plugins.find(p => p.name === pluginName);
    if (plugin && typeof plugin.activate === 'function') {
      try {
        plugin.activate(args);
        console.log(`Activated plugin: ${pluginName}`);
      } catch (error) {
        console.error(`Error activating plugin: ${pluginName}`, error);
      }
    } else {
      console.warn(`Plugin not found or cannot be activated: ${pluginName}`);
    }
  }

  deactivatePlugin(pluginName) {
    const plugin = this.plugins.find(p => p.name === pluginName);
    if (plugin && typeof plugin.deactivate === 'function') {
      try {
        plugin.deactivate();
        console.log(`Deactivated plugin: ${pluginName}`);
      } catch (error) {
        console.error(`Error deactivating plugin: ${pluginName}`, error);
      }
    } else {
      console.warn(`Plugin not found or cannot be deactivated: ${pluginName}`);
    }
  }
}

const pluginManager = new PluginManager();
const PLUGIN_DIRECTORY = path.join(__dirname, 'plugins');

// Load all plugins on startup
pluginManager.loadPlugins(PLUGIN_DIRECTORY);

document.addEventListener('DOMContentLoaded', () => {
  const navigationPane = document.getElementById('navigation-pane');
  const docArea = document.getElementById('doc-area');
  const terminalChat = document.getElementById('terminal-chat');

  // Example UI interaction: Open a new document
  document.getElementById('open-doc-button').addEventListener('click', () => {
    ipcRenderer.send('open-document');
  });

  // Handle plugin activation/deactivation from the navigation pane
  navigationPane.addEventListener('click', (event) => {
    const target = event.target;
    if (target.classList.contains('plugin-item')) {
      const pluginName = target.dataset.pluginName;
      const action = target.dataset.action;

      if (action === 'activate') {
        pluginManager.activatePlugin(pluginName);
      } else if (action === 'deactivate') {
        pluginManager.deactivatePlugin(pluginName);
      }
    }
  });

  // Handle chat interactions
  document.getElementById('chat-input').addEventListener('keypress', (event) => {
    if (event.key === 'Enter') {
      const inputField = event.target;
      const message = inputField.value.trim();
      if (message) {
        ipcRenderer.send('send-chat-message', { content: message });
        inputField.value = '';
      }
    }
  });

  // Listen for chat messages from the main process
  ipcRenderer.on('receive-chat-message', (_, message) => {
    const messageElement = document.createElement('div');
    messageElement.textContent = message.content;
    terminalChat.appendChild(messageElement);
  });
});