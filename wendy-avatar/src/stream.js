/**
 * stream.js - WebSocket connection to brain feed
 *
 * Connects to wendy-sites /ws/brain endpoint and emits parsed events.
 */

export class BrainStream extends EventTarget {
    constructor(wsUrl, authUrl = null) {
        super();
        this.wsUrl = wsUrl;
        this.authUrl = authUrl || wsUrl.replace('wss://', 'https://').replace('ws://', 'http://').replace('/ws/brain', '/api/brain/auth');
        this.ws = null;
        this.token = null;
        this.reconnectDelay = 1000;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 1;  // Prompt for re-auth quickly
        this.eventCount = 0;
    }

    /**
     * Authenticate with the brain feed
     * @param {string} code - Access code
     * @returns {Promise<boolean>}
     */
    async authenticate(code) {
        const response = await fetch(this.authUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || 'Invalid code');
        }

        const { token } = await response.json();
        this.token = token;
        localStorage.setItem('brain_token', token);
        return true;
    }

    /**
     * Try to use stored token
     * @returns {boolean}
     */
    loadStoredToken() {
        this.token = localStorage.getItem('brain_token');
        return !!this.token;
    }

    /**
     * Clear stored token
     */
    clearToken() {
        this.token = null;
        localStorage.removeItem('brain_token');
    }

    /**
     * Connect to WebSocket
     */
    connect() {
        if (!this.token) {
            throw new Error('Not authenticated');
        }

        const url = `${this.wsUrl}?token=${encodeURIComponent(this.token)}`;
        this.ws = new WebSocket(url);

        this.ws.onopen = () => {
            this.reconnectAttempts = 0;  // Reset on successful connection
            this.dispatchEvent(new CustomEvent('connected'));
        };

        this.ws.onclose = (e) => {
            this.dispatchEvent(new CustomEvent('disconnected', { detail: { code: e.code } }));

            // Auth-related close codes
            const authErrorCodes = [4001, 4003, 1008, 3000];
            if (authErrorCodes.includes(e.code)) {
                // Token invalid or expired
                console.log(`Auth error (code ${e.code}), clearing token`);
                this.clearToken();
                this.dispatchEvent(new CustomEvent('auth_required'));
            } else {
                // Track reconnect attempts
                this.reconnectAttempts++;
                if (this.reconnectAttempts >= this.maxReconnectAttempts) {
                    console.log(`Max reconnect attempts (${this.maxReconnectAttempts}) reached, requesting re-auth`);
                    this.clearToken();
                    this.dispatchEvent(new CustomEvent('auth_required'));
                } else {
                    // Reconnect with exponential backoff
                    const delay = this.reconnectDelay * this.reconnectAttempts;
                    console.log(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);
                    setTimeout(() => this.connect(), delay);
                }
            }
        };

        this.ws.onerror = () => {
            this.dispatchEvent(new CustomEvent('error'));
        };

        this.ws.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data);

                // Skip ping
                if (data.type === 'ping') {
                    this.ws.send('pong');
                    return;
                }

                // Check for auth error messages
                if (data.type === 'error' && (data.code === 'auth_failed' || data.message?.includes('token') || data.message?.includes('auth'))) {
                    console.log('Auth error in message:', data);
                    this.clearToken();
                    this.dispatchEvent(new CustomEvent('auth_required'));
                    return;
                }

                this.eventCount++;
                this.dispatchEvent(new CustomEvent('event', { detail: data }));
            } catch (err) {
                console.error('Failed to parse event:', err);
            }
        };
    }

    /**
     * Disconnect WebSocket
     */
    disconnect() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }

    /**
     * Check if connected
     * @returns {boolean}
     */
    get connected() {
        return this.ws?.readyState === WebSocket.OPEN;
    }
}

/**
 * Parse a brain feed event into a classified activity
 * @param {object} data - Raw event from stream
 * @returns {object} - { type, subtype, content, raw }
 */
export function classifyEvent(data) {
    const event = data.event || {};
    const result = {
        type: 'unknown',
        subtype: null,
        content: null,
        raw: data
    };

    // System events
    if (event.type === 'system') {
        result.type = 'system';
        result.subtype = event.subtype; // 'init'
        return result;
    }

    // Result/completion
    if (event.type === 'result') {
        result.type = 'result';
        result.subtype = event.is_error ? 'error' : 'success';
        result.content = event.result;
        return result;
    }

    // Assistant message (tool use or text)
    if (event.type === 'assistant') {
        const content = event.message?.content || [];

        for (const block of content) {
            if (block.type === 'text') {
                result.type = 'thinking';
                result.content = block.text;
                return result;
            }

            if (block.type === 'tool_use') {
                result.type = 'tool_use';
                result.subtype = block.name;
                result.content = block.input;
                result.toolId = block.id;

                // Classify Bash commands
                if (block.name === 'Bash') {
                    const cmd = block.input?.command || '';
                    if (cmd.includes('check_messages')) {
                        result.subtype = 'check_messages';
                    } else if (cmd.includes('send_message')) {
                        result.subtype = 'send_message';
                        // Extract message content from curl
                        result.messageContent = extractMessageFromCurl(cmd);
                    }
                }

                return result;
            }
        }
    }

    // Tool result
    if (event.type === 'user') {
        const content = event.message?.content || [];

        for (const block of content) {
            if (block.type === 'tool_result') {
                result.type = 'tool_result';
                result.content = block.content;
                result.isError = block.is_error;
                result.toolId = block.tool_use_id;
                return result;
            }
        }
    }

    return result;
}

/**
 * Extract message content from send_message curl command
 */
function extractMessageFromCurl(cmd) {
    // Try multiple extraction methods

    // Method 1: Look for "content": "..." pattern directly
    // Handles escaped quotes inside the content
    const contentMatch = cmd.match(/"content"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (contentMatch) {
        // Unescape the string
        return contentMatch[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\');
    }

    // Method 2: Try to find -d argument and parse as JSON
    // Match -d followed by single or double quoted JSON (greedy)
    const dArgMatch = cmd.match(/-d\s*'([^']+)'/s) || cmd.match(/-d\s*"([^"]+)"/s);
    if (dArgMatch) {
        try {
            const body = JSON.parse(dArgMatch[1]);
            return body.content;
        } catch {
            // JSON parse failed, continue
        }
    }

    // Method 3: heredoc style -d $'...'
    const heredocMatch = cmd.match(/-d\s*\$'([^']+)'/s);
    if (heredocMatch) {
        try {
            const body = JSON.parse(heredocMatch[1].replace(/\\'/g, "'"));
            return body.content;
        } catch {
            // JSON parse failed
        }
    }

    return null;
}
