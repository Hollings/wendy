/**
 * monitor.js - Chat-focused monitor renderer
 *
 * Base layer: Chat messages from Discord
 * Overlays: Terminal output, diffs, etc. (rendered on top)
 *
 * Typing is character-by-character, synced with keyboard presses.
 */

// =============================================================================
// Configuration
// =============================================================================

const COLORS = {
    background: '#1a1a1a',
    text: '#e0e0e0',
    textDim: '#888888',
    textMuted: '#555555',
    author: '#60a5fa',
    authorWendy: '#f472b6',
    cursor: '#f472b6',
};

const LAYOUT = {
    padding: 20,
    lineHeight: 28,
    messageGap: 16,
    fontSize: 22,
    authorFontSize: 18,
};

// =============================================================================
// Monitor Class
// =============================================================================

export class Monitor {
    constructor(canvas, texture) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.texture = texture;
        this.width = canvas.width;
        this.height = canvas.height;

        // Chat state
        this.messages = [];
        this.maxMessages = 20;

        // Wendy's typing state (null when not typing)
        this.typing = null;  // { author, fullText, visibleText }

        // Cursor blink
        this.cursorVisible = true;
        this.cursorBlinkTime = 0;

        // Initial render
        this.render();
    }

    // =========================================================================
    // Public API
    // =========================================================================

    /**
     * Set the message list (typically from check_messages response)
     * @param {Array<{author: string, content: string}>} messages
     */
    setMessages(messages) {
        this.messages = messages.slice(-this.maxMessages);
        this.render();
    }

    /**
     * Add a single message to the chat
     * @param {string} author
     * @param {string} content
     */
    addMessage(author, content) {
        this.messages.push({ author, content });
        if (this.messages.length > this.maxMessages) {
            this.messages.shift();
        }
        this.render();
    }

    /**
     * Start Wendy typing a message
     * @param {string} fullText - The complete message she will type
     */
    startTyping(fullText) {
        this.typing = {
            author: 'Wendy',
            fullText,
            visibleText: '',
        };
        this.render();
    }

    /**
     * Add a character to Wendy's typing (call when key is pressed)
     * @param {string} char - Character to add
     */
    typeChar(char) {
        if (!this.typing) return;

        this.typing.visibleText += char;
        this.cursorVisible = true;  // Reset cursor on keypress
        this.cursorBlinkTime = 0;
        this.render();
    }

    /**
     * Finish typing - add message to chat and clear typing state
     */
    finishTyping() {
        if (!this.typing) return;

        this.addMessage(this.typing.author, this.typing.fullText);
        this.typing = null;
        this.render();
    }

    /**
     * Check if Wendy is currently typing
     * @returns {boolean}
     */
    isTyping() {
        return this.typing !== null;
    }

    /**
     * Check if typing is complete (all characters typed)
     * @returns {boolean}
     */
    isTypingComplete() {
        if (!this.typing) return false;
        return this.typing.visibleText.length >= this.typing.fullText.length;
    }

    /**
     * Update animations (call from render loop)
     * @param {number} delta - Time since last frame in seconds
     */
    update(delta) {
        // Cursor blink
        this.cursorBlinkTime += delta;
        if (this.cursorBlinkTime >= 0.5) {
            this.cursorBlinkTime = 0;
            this.cursorVisible = !this.cursorVisible;
            if (this.typing) {
                this.render();
            }
        }
    }

    // =========================================================================
    // Rendering
    // =========================================================================

    /**
     * Main render function - draws chat and any overlays
     */
    render() {
        this.clear();
        this.drawChat();
        this.markDirty();
    }

    clear() {
        this.ctx.fillStyle = COLORS.background;
        this.ctx.fillRect(0, 0, this.width, this.height);
    }

    markDirty() {
        this.texture.needsUpdate = true;
    }

    /**
     * Draw the chat messages from bottom to top
     */
    drawChat() {
        const { padding, lineHeight, messageGap, fontSize, authorFontSize } = LAYOUT;
        const maxWidth = this.width - padding * 2;

        this.ctx.textBaseline = 'top';

        // Build list of messages to render (including typing)
        const renderMessages = [...this.messages];

        // Add typing message at the end if active
        if (this.typing) {
            renderMessages.push({
                author: this.typing.author,
                content: this.typing.visibleText,
                isTyping: true,
            });
        }

        // Calculate layout for each message (author line + wrapped content)
        const layouts = renderMessages.map(msg => {
            this.ctx.font = `${fontSize}px monospace`;
            const lines = this.wrapText(msg.content || ' ', maxWidth);
            const height = authorFontSize + 4 + (lines.length * lineHeight);
            return { msg, lines, height };
        });

        // Draw from bottom up
        let y = this.height - padding;

        for (let i = layouts.length - 1; i >= 0; i--) {
            const { msg, lines, height } = layouts[i];

            // Check if we have room
            if (y - height < padding) break;

            y -= height;

            // Author name
            this.ctx.font = `${authorFontSize}px monospace`;
            this.ctx.fillStyle = msg.author === 'Wendy' ? COLORS.authorWendy : COLORS.author;
            this.ctx.fillText(msg.author, padding, y);

            // Message content
            this.ctx.font = `${fontSize}px monospace`;
            this.ctx.fillStyle = COLORS.text;

            let lineY = y + authorFontSize + 4;
            for (let j = 0; j < lines.length; j++) {
                this.ctx.fillText(lines[j], padding, lineY);

                // Draw cursor at end of last line if typing
                if (msg.isTyping && j === lines.length - 1 && this.cursorVisible) {
                    const cursorX = padding + this.ctx.measureText(lines[j]).width + 2;
                    this.ctx.fillStyle = COLORS.cursor;
                    this.ctx.fillRect(cursorX, lineY, 3, lineHeight - 4);
                    this.ctx.fillStyle = COLORS.text;
                }

                lineY += lineHeight;
            }

            y -= messageGap;
        }

        // Draw "empty" state if no messages
        if (renderMessages.length === 0) {
            this.ctx.font = `${fontSize}px monospace`;
            this.ctx.fillStyle = COLORS.textMuted;
            this.ctx.textAlign = 'center';
            this.ctx.fillText('No messages', this.width / 2, this.height / 2);
            this.ctx.textAlign = 'left';
        }
    }

    /**
     * Wrap text to fit within maxWidth
     * @param {string} text
     * @param {number} maxWidth
     * @returns {string[]}
     */
    wrapText(text, maxWidth) {
        if (!text) return [''];

        const words = text.split(' ');
        const lines = [];
        let currentLine = '';

        for (const word of words) {
            const testLine = currentLine ? `${currentLine} ${word}` : word;
            const width = this.ctx.measureText(testLine).width;

            if (width > maxWidth && currentLine) {
                lines.push(currentLine);
                currentLine = word;
            } else {
                currentLine = testLine;
            }
        }

        if (currentLine) {
            lines.push(currentLine);
        }

        return lines.length ? lines : [''];
    }
}
