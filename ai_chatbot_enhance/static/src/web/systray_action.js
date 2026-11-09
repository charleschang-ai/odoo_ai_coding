import {Component, useState, onWillStart} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";
import {user} from "@web/core/user";
import {Dropdown} from "@web/core/dropdown/dropdown";
import {DropdownItem} from "@web/core/dropdown/dropdown_item";

export default class SystrayAction extends Component {
    static props = {};
    static template = "ai.en.SystrayAction";
    static components = {Dropdown, DropdownItem};

    setup() {
        super.setup();
        this.actionService = useService("action");
        this.aiChatLauncher = useService("simpleAIChat");
        this.orm = useService("orm");

        this.state = useState({
            agents: [],
            isAiUser: false,
        });

        onWillStart(async () => {
            this.state.isAiUser = await user.hasGroup("ai_chatbot_enhance.group_ai_user");
            if (this.state.isAiUser) {
                await this.loadAgents();
            }
        });
    }

    async onDropdownOpened() {
        await this.loadAgents();
    }

    async loadAgents() {
        try {
            // Server-side filter: only agents the user may actually chat with
            const agents = await this.orm.call("ai.agent.bot", "ai_get_available_agents", []);

            this.state.agents = agents;

        } catch (error) {
            console.error("Pull AI Agent list failed:", error);
        }
    }

    async onClickLaunchAIChat(agent) {
        await this.aiChatLauncher.launchAIChat({
            callerComponentName: "systray_ai_button",
            agentId: agent.id,
            agentName: agent.name
        });
    }
}

registry.category("systray").add("ai.en.systray_action", {Component: SystrayAction}, {sequence: 30});