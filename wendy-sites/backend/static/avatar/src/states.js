/**
 * states.js - State machine for Wendy's activity
 *
 * States:
 * - idle: No active session, sleeping/resting
 * - waking: Session starting, waking up
 * - check_messages: Reading Discord messages
 * - thinking: Processing, thought bubble shown
 * - terminal: Running bash commands
 * - editing: Editing files
 * - read_file: Reading file contents
 * - read_image: Viewing an image
 * - send_message: Sending Discord message (typing animation)
 * - done: Session complete, transitioning to idle
 */

export const STATES = {
    IDLE: 'idle',
    WAKING: 'waking',
    CHECK_MESSAGES: 'check_messages',
    THINKING: 'thinking',
    TERMINAL: 'terminal',
    EDITING: 'editing',
    READ_FILE: 'read_file',
    READ_IMAGE: 'read_image',
    SEND_MESSAGE: 'send_message',
    DONE: 'done',
};

export class StateMachine extends EventTarget {
    /**
     * @param {TypingAnimator} typingAnimator - Shared typing animator instance
     */
    constructor(typingAnimator) {
        super();
        this.state = STATES.IDLE;
        this.stateData = {};
        this.idleTimeout = null;

        // Pending send_message (waiting for success response)
        this.pendingSendMessage = null;

        // Typing animator for blocking transitions
        this.typingAnimator = typingAnimator;
    }

    /**
     * Get current state
     */
    get currentState() {
        return this.state;
    }

    /**
     * Transition to a new state
     */
    transition(newState, data = {}) {
        // Block transitions while typing animation is playing
        if (this.typingAnimator?.isBlocking() && newState !== STATES.SEND_MESSAGE) {
            const remaining = this.typingAnimator.getRemainingTime();
            setTimeout(() => this.transition(newState, data), remaining + 100);
            return;
        }

        const oldState = this.state;
        this.state = newState;
        this.stateData = data;

        // Clear idle timeout on activity
        if (this.idleTimeout) {
            clearTimeout(this.idleTimeout);
            this.idleTimeout = null;
        }

        // Auto-transition to idle after done
        if (newState === STATES.DONE) {
            this.idleTimeout = setTimeout(() => {
                this.transition(STATES.IDLE);
            }, 2000);
        }

        this.dispatchEvent(new CustomEvent('transition', {
            detail: { from: oldState, to: newState, data }
        }));
    }

    /**
     * Process a classified event and update state
     */
    processEvent(classified) {
        const { type, subtype, content } = classified;

        switch (type) {
            case 'system':
                if (subtype === 'init') {
                    this.transition(STATES.WAKING);
                }
                break;

            case 'result':
                this.transition(STATES.DONE, { success: subtype === 'success' });
                break;

            case 'thinking':
                this.transition(STATES.THINKING, { text: content });
                break;

            case 'tool_use':
                this.handleToolUse(subtype, content, classified);
                break;

            case 'tool_result':
                this.handleToolResult(content, classified.isError);
                break;
        }
    }

    /**
     * Handle tool_use events
     */
    handleToolUse(toolName, input, classified) {
        switch (toolName) {
            case 'check_messages':
                this.transition(STATES.CHECK_MESSAGES, { command: input?.command });
                break;

            case 'send_message':
                // Don't transition yet - wait for success response
                this.pendingSendMessage = {
                    command: input?.command,
                    messageContent: classified.messageContent
                };
                break;

            case 'Bash':
                this.transition(STATES.TERMINAL, { command: input?.command });
                break;

            case 'Edit':
                this.transition(STATES.EDITING, {
                    filePath: input?.file_path,
                    oldString: input?.old_string,
                    newString: input?.new_string
                });
                break;

            case 'Read':
                const filePath = input?.file_path || '';
                const isImage = /\.(png|jpg|jpeg|gif|webp|svg)$/i.test(filePath);
                this.transition(isImage ? STATES.READ_IMAGE : STATES.READ_FILE, { filePath });
                break;

            case 'Write':
                this.transition(STATES.EDITING, {
                    filePath: input?.file_path,
                    content: input?.content,
                    isNew: true
                });
                break;

            case 'Grep':
            case 'Glob':
                this.transition(STATES.TERMINAL, {
                    command: `${toolName}: ${input?.pattern || ''}`,
                    isSearch: true
                });
                break;

            default:
                this.transition(STATES.TERMINAL, {
                    command: toolName,
                    input
                });
        }
    }

    /**
     * Handle tool_result events
     */
    handleToolResult(content, isError) {
        // Check if this is a send_message result
        if (this.pendingSendMessage) {
            const sendData = this.pendingSendMessage;
            this.pendingSendMessage = null;

            // Check if message was sent successfully
            const isSuccess = !isError && content &&
                (content.includes('success') || content.includes('queued'));

            if (isSuccess) {
                this.transition(STATES.SEND_MESSAGE, {
                    messageContent: sendData.messageContent,
                    startTyping: true
                });
                return;
            }
        }

        // Update current state with result
        const data = { ...this.stateData, result: content, isError };

        if (this.state === STATES.CHECK_MESSAGES) {
            data.messages = parseCheckMessagesResult(content);
        }

        this.stateData = data;
        this.dispatchEvent(new CustomEvent('result', {
            detail: { state: this.state, data }
        }));
    }
}

/**
 * Parse check_messages result to extract messages
 */
function parseCheckMessagesResult(content) {
    if (!content) return [];

    try {
        const data = typeof content === 'string' ? JSON.parse(content) : content;
        if (data.messages && Array.isArray(data.messages)) {
            return data.messages.map(m => ({
                author: m.author,
                content: m.content,
                timestamp: m.timestamp
            }));
        }
    } catch {
        // Not JSON
    }

    return [];
}
