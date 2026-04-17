// multi-agent-system.js

class MultiAgentSystem {
    constructor() {
        this.agents = [];
        this.taskQueue = [];
        this.isProcessing = false;
    }

    initializeAgents(agentCount) {
        for (let i = 0; i < agentCount; i++) {
            const agentId = `agent-${i}`;
            const agent = new Agent(agentId);
            this.agents.push(agent);
        }
    }

    enqueueTask(task) {
        this.taskQueue.push(task);
        if (!this.isProcessing) {
            this.processTasks();
        }
    }

    processTasks() {
        this.isProcessing = true;

        while (this.taskQueue.length > 0 && this.agents.some(agent => !agent.busy)) {
            const task = this.taskQueue.shift();
            const availableAgent = this.agents.find(agent => !agent.busy);

            if (availableAgent) {
                availableAgent.assignTask(task);
                availableAgent.executeTask(() => {
                    availableAgent.completeTask();
                    if (this.taskQueue.length > 0) {
                        this.processTasks();
                    } else {
                        this.isProcessing = false;
                    }
                });
            } else {
                // No agents are currently free, re-enqueue the task
                this.taskQueue.unshift(task);
                break;
            }
        }

        if (this.taskQueue.length === 0 && !this.agents.some(agent => agent.busy)) {
            this.isProcessing = false;
        }
    }
}

class Agent {
    constructor(id) {
        this.id = id;
        this.busy = false;
        this.currentTask = null;
    }

    assignTask(task) {
        this.currentTask = task;
        this.busy = true;
    }

    executeTask(callback) {
        console.log(`Executing task ${this.currentTask.id} on ${this.id}`);
        
        // Simulate task execution with a timeout
        setTimeout(() => {
            callback();
        }, Math.random() * 2000 + 1000); // Random delay between 1 and 3 seconds
    }

    completeTask() {
        console.log(`Completed task ${this.currentTask.id} on ${this.id}`);
        this.busy = false;
        this.currentTask = null;
    }
}

// Example usage:
const multiAgentSystem = new MultiAgentSystem();
multiAgentSystem.initializeAgents(4);

for (let i = 0; i < 10; i++) {
    const task = { id: `task-${i}` };
    multiAgentSystem.enqueueTask(task);
}