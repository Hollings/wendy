/**
 * effects.js - Post-processing effects manager
 *
 * Manages visual styles that can be toggled at runtime.
 */

import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

import { CRTShader, PS1Shader, VHSShader, NoirShader, PixelateShader } from './shaders.js';

// =============================================================================
// Visual Styles
// =============================================================================

export const STYLES = {
    CLEAN: 'clean',
    CRT: 'crt',
    PS1: 'ps1',
    VHS: 'vhs',
    NOIR: 'noir',
};

const STYLE_ORDER = [STYLES.CLEAN, STYLES.CRT, STYLES.PS1, STYLES.VHS, STYLES.NOIR];

const STYLE_NAMES = {
    [STYLES.CLEAN]: 'Clean',
    [STYLES.CRT]: 'CRT Monitor',
    [STYLES.PS1]: 'PlayStation 1',
    [STYLES.VHS]: 'VHS Tape',
    [STYLES.NOIR]: 'Film Noir',
};

// =============================================================================
// Effects Manager
// =============================================================================

export class EffectsManager {
    constructor(renderer, scene, camera) {
        this.renderer = renderer;
        this.scene = scene;
        this.camera = camera;

        this.currentStyle = STYLES.CLEAN;
        this.composer = null;
        this.shaderPasses = {};
        this.time = 0;

        // Animation frame rate limiting for PS1 style
        this.targetFPS = 60;
        this.frameAccumulator = 0;
        this.lastRenderTime = 0;

        this.init();
    }

    init() {
        // Create effect composer
        this.composer = new EffectComposer(this.renderer);

        // Base render pass
        const renderPass = new RenderPass(this.scene, this.camera);
        this.composer.addPass(renderPass);

        // Get resolution for shaders
        const resolution = new THREE.Vector2(
            this.renderer.domElement.width,
            this.renderer.domElement.height
        );

        // Create shader passes for each style
        this.shaderPasses.crt = new ShaderPass(CRTShader);

        this.shaderPasses.ps1 = new ShaderPass(PS1Shader);
        this.shaderPasses.ps1.uniforms.resolution.value = resolution;

        this.shaderPasses.vhs = new ShaderPass(VHSShader);

        this.shaderPasses.noir = new ShaderPass(NoirShader);

        this.shaderPasses.pixelate = new ShaderPass(PixelateShader);
        this.shaderPasses.pixelate.uniforms.resolution.value = resolution;

        // Output pass (tone mapping, color space conversion)
        this.outputPass = new OutputPass();
        this.composer.addPass(this.outputPass);

        // Start with CRT by default
        this.setStyle(STYLES.CRT);
    }

    /**
     * Set the current visual style
     * @param {string} style - One of STYLES values
     */
    setStyle(style) {
        if (!STYLE_ORDER.includes(style)) {
            console.warn('Unknown style:', style);
            return;
        }

        this.currentStyle = style;

        // Remove all shader passes (keep render pass and output pass)
        while (this.composer.passes.length > 2) {
            this.composer.removePass(this.composer.passes[1]);
        }

        // Add the appropriate shader pass
        switch (style) {
            case STYLES.CRT:
                this.composer.insertPass(this.shaderPasses.crt, 1);
                this.targetFPS = 60;
                break;

            case STYLES.PS1:
                this.composer.insertPass(this.shaderPasses.ps1, 1);
                // PS1 had lower framerates - limit to 15fps for authentic feel
                this.targetFPS = 15;
                break;

            case STYLES.VHS:
                this.composer.insertPass(this.shaderPasses.vhs, 1);
                this.targetFPS = 30;  // VHS was ~30fps
                break;

            case STYLES.NOIR:
                this.composer.insertPass(this.shaderPasses.noir, 1);
                this.targetFPS = 24;  // Film is 24fps
                break;

            case STYLES.CLEAN:
            default:
                this.targetFPS = 60;
                break;
        }

        console.log(`Style set to: ${STYLE_NAMES[style]} (${this.targetFPS}fps)`);

        // Dispatch event for UI updates
        window.dispatchEvent(new CustomEvent('stylechange', {
            detail: { style, name: STYLE_NAMES[style] }
        }));
    }

    /**
     * Cycle to the next style
     * @returns {string} The new style
     */
    nextStyle() {
        const currentIndex = STYLE_ORDER.indexOf(this.currentStyle);
        const nextIndex = (currentIndex + 1) % STYLE_ORDER.length;
        this.setStyle(STYLE_ORDER[nextIndex]);
        return this.currentStyle;
    }

    /**
     * Get current style name for display
     * @returns {string}
     */
    getStyleName() {
        return STYLE_NAMES[this.currentStyle];
    }

    /**
     * Update shader uniforms and handle frame rate limiting
     * @param {number} delta - Time since last frame in seconds
     * @returns {boolean} - True if should render this frame
     */
    update(delta) {
        this.time += delta;

        // Update time uniforms for all active shaders
        for (const pass of Object.values(this.shaderPasses)) {
            if (pass.uniforms.time) {
                pass.uniforms.time.value = this.time;
            }
        }

        // Frame rate limiting for retro styles
        if (this.targetFPS < 60) {
            this.frameAccumulator += delta;
            const frameTime = 1.0 / this.targetFPS;

            if (this.frameAccumulator < frameTime) {
                return false;  // Skip this frame
            }

            this.frameAccumulator -= frameTime;
        }

        return true;
    }

    /**
     * Render the scene with current effects
     */
    render() {
        this.composer.render();
    }

    /**
     * Handle window resize
     * @param {number} width
     * @param {number} height
     */
    resize(width, height) {
        this.composer.setSize(width, height);

        const resolution = new THREE.Vector2(width, height);

        if (this.shaderPasses.ps1) {
            this.shaderPasses.ps1.uniforms.resolution.value = resolution;
        }
        if (this.shaderPasses.pixelate) {
            this.shaderPasses.pixelate.uniforms.resolution.value = resolution;
        }
    }

    /**
     * Get all available styles for UI
     * @returns {Array<{id: string, name: string}>}
     */
    static getAvailableStyles() {
        return STYLE_ORDER.map(id => ({
            id,
            name: STYLE_NAMES[id],
        }));
    }
}
