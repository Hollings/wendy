/**
 * chat-manager.js - Centralized chat state management
 *
 * Responsibilities:
 * - Track messages per channel with deduplication
 * - Manage typing state with proper guards
 * - Handle initial load vs live updates
 * - Coordinate between stream events and display
 */

// =============================================================================
// ChatManager Class
// =============================================================================

export class ChatManager {
    /**
     * @param {object} options
     * @param {Monitor} options.monitor - Monitor instance for display
     * @param {function} options.onTypeChar - Callback for each character typed (for keyboard sync)
     * @param {function} options.onTypingStart - Callback when typing starts
     * @param {function} options.onTypingEnd - Callback when typing ends
     */
    constructor({ monitor, onTypeChar, onTypingStart, onTypingEnd }) {
        this.monitor = monitor;
        this.onTypeChar = onTypeChar || (() => {});
        this.onTypingStart = onTypingStart || (() => {});
        this.onTypingEnd = onTypingEnd || (() => {});

        // Per-channel message storage: channelId -> { messages: Map<msgId, msg>, order: [] }
        this.channels = new Map();
        this.currentChannelId = null;

        // Typing state
        this.typing = {
            active: false,
            text: '',
            index: 0,
            aborted: false,
            resolve: null,  // Promise resolver for when typing completes
        };

        // Session tracking
        this.sessionCount = 0;  // Increments each session, used for initial load detection
        this.lastProcessedSession = -1;

        // Pending message (from send_message tool_use, waiting for success)
        this.pendingMessage = null;
    }

    // =========================================================================
    // Channel Management
    // =========================================================================

    /**
     * Set the current active channel
     * @param {string} channelId
     */
    setCurrentChannel(channelId) {
        if (this.currentChannelId === channelId) return;

        this.currentChannelId = channelId;

        // Initialize channel if needed
        if (!this.channels.has(channelId)) {
            this.channels.set(channelId, {
                messages: new Map(),
                order: [],  // Message IDs in display order
            });
        }

        // Update display
        this.syncDisplay();
    }

    /**
     * Get or create channel data
     * @param {string} channelId
     * @returns {object}
     */
    getChannel(channelId) {
        if (!this.channels.has(channelId)) {
            this.channels.set(channelId, {
                messages: new Map(),
                order: [],
            });
        }
        return this.channels.get(channelId);
    }

    // =========================================================================
    // Message Handling
    // =========================================================================

    /**
     * Process messages from check_messages response
     * @param {string} channelId - Channel these messages are from
     * @param {Array<{message_id, author, content, timestamp}>} messages
     * @param {boolean} isInitialLoad - If true, don't animate
     */
    receiveMessages(channelId, messages, isInitialLoad = false) {
        if (!messages || !Array.isArray(messages)) return;

        const channel = this.getChannel(channelId);
        let hasNew = false;

        for (const msg of messages) {
            const id = msg.message_id || `${msg.author}-${msg.timestamp}-${msg.content?.slice(0, 20)}`;

            // Skip if already have this message
            if (channel.messages.has(id)) continue;

            hasNew = true;
            channel.messages.set(id, {
                id,
                author: msg.author,
                content: msg.content,
                timestamp: msg.timestamp,
            });
            channel.order.push(id);
        }

        // Trim old messages
        while (channel.order.length > 50) {
            const oldId = channel.order.shift();
            channel.messages.delete(oldId);
        }

        // Update display if this is the current channel
        if (hasNew && channelId === this.currentChannelId) {
            this.syncDisplay();
        }
    }

    /**
     * Queue a message for Wendy to type (from send_message tool_use)
     * @param {string} channelId
     * @param {string} content
     */
    queueWendyMessage(channelId, content) {
        if (!content) return;

        this.pendingMessage = {
            channelId,
            content,
        };
    }

    /**
     * Confirm pending message was sent successfully
     * @param {boolean} isInitialLoad - If true, just add to chat without typing
     * @returns {Promise} Resolves when typing is complete
     */
    async confirmMessageSent(isInitialLoad = false) {
        if (!this.pendingMessage) return;

        const { channelId, content } = this.pendingMessage;
        this.pendingMessage = null;

        // Set channel if needed
        if (channelId) {
            this.setCurrentChannel(channelId);
        }

        if (isInitialLoad) {
            // Just add to chat without animation
            this.addWendyMessage(content);
        } else {
            // Type with animation
            await this.typeMessage(content);
        }
    }

    /**
     * Add a Wendy message directly (no typing animation)
     * @param {string} content
     */
    addWendyMessage(content) {
        if (!this.currentChannelId) return;

        const channel = this.getChannel(this.currentChannelId);
        const id = `wendy-${Date.now()}`;

        channel.messages.set(id, {
            id,
            author: 'Wendy',
            content,
            timestamp: Date.now() / 1000,
        });
        channel.order.push(id);

        this.syncDisplay();
    }

    // =========================================================================
    // Typing Animation
    // =========================================================================

    /**
     * Type a message with animation
     * @param {string} text
     * @param {number} charDelay - Ms between characters (time for keyboard IK)
     * @returns {Promise} Resolves when typing completes
     */
    async typeMessage(text, charDelay = 150) {
        console.log('[DEBUG] ChatManager.typeMessage() called:', {
            text: text?.slice(0, 50),
            charDelay,
            currentlyTyping: this.typing.active,
        });

        // Don't start if already typing
        if (this.typing.active) {
            console.warn('ChatManager: Already typing, ignoring new message');
            return;
        }

        if (!text) {
            console.log('[DEBUG] ChatManager.typeMessage() - no text, returning');
            return;
        }

        console.log('[DEBUG] ChatManager.typeMessage() - starting typing animation');

        this.typing = {
            active: true,
            text,
            index: 0,
            aborted: false,
            resolve: null,
        };

        // Notify start
        this.onTypingStart(text);

        // Start typing on monitor (keyboard IK will update characters)
        if (this.monitor) {
            this.monitor.startTyping(text);
        }

        // Create promise for completion
        const promise = new Promise(resolve => {
            this.typing.resolve = resolve;
        });

        // Queue each character for keyboard IK (IK updates monitor on press)
        for (let i = 0; i < text.length; i++) {
            if (this.typing.aborted) break;

            const char = text[i];
            this.typing.index = i;

            // Notify callback (queues keyboard IK movement)
            this.onTypeChar(char);

            // Wait for keyboard IK to complete this character
            await this.sleep(charDelay);
        }

        // Wait a bit for last character to finish, then complete
        await this.sleep(300);
        this.finishTyping();

        return promise;
    }

    /**
     * Advance typing by one character (called from keyboard sync)
     * @returns {string|null} The character typed, or null if not typing
     */
    advanceTyping() {
        if (!this.typing.active) return null;

        const char = this.typing.text[this.typing.index];
        if (!char) {
            this.finishTyping();
            return null;
        }

        this.typing.index++;

        // Update monitor
        if (this.monitor) {
            this.monitor.typeChar(char);
        }

        // Check if done
        if (this.typing.index >= this.typing.text.length) {
            this.finishTyping();
        }

        return char;
    }

    /**
     * Finish typing and add message to chat
     */
    finishTyping() {
        if (!this.typing.active) return;

        const text = this.typing.text;

        // Clear typing state
        const resolve = this.typing.resolve;
        this.typing = {
            active: false,
            text: '',
            index: 0,
            aborted: false,
            resolve: null,
        };

        // Finish on monitor (this adds the message to monitor's display)
        if (this.monitor) {
            this.monitor.finishTyping();
        }

        // Also add to our channel tracking (without re-adding to monitor)
        if (this.currentChannelId) {
            const channel = this.getChannel(this.currentChannelId);
            const id = `wendy-${Date.now()}`;
            channel.messages.set(id, {
                id,
                author: 'Wendy',
                content: text,
                timestamp: Date.now() / 1000,
            });
            channel.order.push(id);
        }

        // Notify end
        this.onTypingEnd();

        // Resolve promise
        if (resolve) resolve();
    }

    /**
     * Abort current typing
     */
    abortTyping() {
        if (!this.typing.active) return;

        this.typing.aborted = true;

        // Clear monitor typing state without adding message
        if (this.monitor && this.monitor.isTyping()) {
            this.monitor.typing = null;
            this.monitor.render();
        }

        // Clear our typing state
        const resolve = this.typing.resolve;
        this.typing = {
            active: false,
            text: '',
            index: 0,
            aborted: false,
            resolve: null,
        };

        // Notify end
        this.onTypingEnd();

        // Resolve promise
        if (resolve) resolve();
    }

    /**
     * Check if currently typing
     * @returns {boolean}
     */
    isTyping() {
        return this.typing.active;
    }

    // =========================================================================
    // Session Management
    // =========================================================================

    /**
     * Called when a new session starts
     */
    onSessionStart() {
        this.sessionCount++;
    }

    /**
     * Called when a session ends
     */
    onSessionEnd() {
        this.lastProcessedSession = this.sessionCount;
    }

    /**
     * Check if this is the initial load (first session)
     * Only true when no session has ever completed (first page load)
     * @returns {boolean}
     */
    isInitialLoad() {
        // Initial load = first session ever (no session has completed yet)
        const result = this.lastProcessedSession === -1;
        console.log('[DEBUG] isInitialLoad():', {
            lastProcessedSession: this.lastProcessedSession,
            sessionCount: this.sessionCount,
            result,
        });
        return result;
    }

    /**
     * Reset state (e.g., on reconnect)
     */
    reset() {
        this.abortTyping();
        this.pendingMessage = null;
        // Keep messages but reset session tracking
        this.sessionCount = 0;
        this.lastProcessedSession = -1;
    }

    // =========================================================================
    // Display
    // =========================================================================

    /**
     * Sync monitor display with current channel messages
     */
    syncDisplay() {
        if (!this.monitor || !this.currentChannelId) return;

        const channel = this.channels.get(this.currentChannelId);
        if (!channel) return;

        // Build message array in order
        const messages = channel.order
            .map(id => channel.messages.get(id))
            .filter(Boolean)
            .map(m => ({
                author: m.author,
                content: m.content,
            }));

        this.monitor.setMessages(messages);
    }

    // =========================================================================
    // Utilities
    // =========================================================================

    sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
}
