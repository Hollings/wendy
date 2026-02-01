/**
 * shaders.js - Custom shader definitions for visual styles
 *
 * Each shader is a post-processing effect that can be applied to the scene.
 */

// =============================================================================
// CRT Shader - Scanlines, barrel distortion, chromatic aberration
// =============================================================================

export const CRTShader = {
    uniforms: {
        tDiffuse: { value: null },
        time: { value: 0 },
        scanlineIntensity: { value: 0.15 },
        scanlineCount: { value: 400.0 },
        vignetteIntensity: { value: 0.3 },
        distortion: { value: 0.03 },
        chromaticAberration: { value: 0.003 },
    },

    vertexShader: /* glsl */ `
        varying vec2 vUv;
        void main() {
            vUv = uv;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
    `,

    fragmentShader: /* glsl */ `
        uniform sampler2D tDiffuse;
        uniform float time;
        uniform float scanlineIntensity;
        uniform float scanlineCount;
        uniform float vignetteIntensity;
        uniform float distortion;
        uniform float chromaticAberration;

        varying vec2 vUv;

        vec2 barrelDistortion(vec2 uv) {
            vec2 centered = uv - 0.5;
            float r2 = dot(centered, centered);
            float distort = 1.0 + r2 * distortion;
            return centered * distort + 0.5;
        }

        void main() {
            vec2 uv = barrelDistortion(vUv);

            // Check bounds after distortion
            if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
                gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0);
                return;
            }

            // Chromatic aberration
            float r = texture2D(tDiffuse, uv + vec2(chromaticAberration, 0.0)).r;
            float g = texture2D(tDiffuse, uv).g;
            float b = texture2D(tDiffuse, uv - vec2(chromaticAberration, 0.0)).b;
            vec3 color = vec3(r, g, b);

            // Scanlines
            float scanline = sin(uv.y * scanlineCount * 3.14159) * 0.5 + 0.5;
            scanline = pow(scanline, 1.5);
            color *= 1.0 - scanlineIntensity * (1.0 - scanline);

            // Subtle flicker
            float flicker = 1.0 - 0.02 * sin(time * 8.0);
            color *= flicker;

            // Vignette
            vec2 vignetteUv = vUv - 0.5;
            float vignette = 1.0 - dot(vignetteUv, vignetteUv) * vignetteIntensity * 2.0;
            color *= vignette;

            // Phosphor glow (slight blur on bright areas)
            float brightness = dot(color, vec3(0.299, 0.587, 0.114));
            color += color * brightness * 0.1;

            gl_FragColor = vec4(color, 1.0);
        }
    `,
};

// =============================================================================
// PS1 Shader - Dithering, color banding, slight jitter
// =============================================================================

export const PS1Shader = {
    uniforms: {
        tDiffuse: { value: null },
        time: { value: 0 },
        resolution: { value: null },
        colorDepth: { value: 32.0 },  // Colors per channel (32 = 5-bit like PS1)
        ditherIntensity: { value: 0.03 },
        jitterAmount: { value: 0.001 },
    },

    vertexShader: /* glsl */ `
        varying vec2 vUv;
        void main() {
            vUv = uv;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
    `,

    fragmentShader: /* glsl */ `
        uniform sampler2D tDiffuse;
        uniform float time;
        uniform vec2 resolution;
        uniform float colorDepth;
        uniform float ditherIntensity;
        uniform float jitterAmount;

        varying vec2 vUv;

        // Bayer 4x4 dithering pattern
        float bayer4(vec2 pos) {
            int x = int(mod(pos.x, 4.0));
            int y = int(mod(pos.y, 4.0));
            int index = x + y * 4;

            // Bayer matrix values (0-15 normalized to 0-1)
            float pattern[16];
            pattern[0] = 0.0; pattern[1] = 8.0; pattern[2] = 2.0; pattern[3] = 10.0;
            pattern[4] = 12.0; pattern[5] = 4.0; pattern[6] = 14.0; pattern[7] = 6.0;
            pattern[8] = 3.0; pattern[9] = 11.0; pattern[10] = 1.0; pattern[11] = 9.0;
            pattern[12] = 15.0; pattern[13] = 7.0; pattern[14] = 13.0; pattern[15] = 5.0;

            return pattern[index] / 16.0 - 0.5;
        }

        void main() {
            // Slight UV jitter for that wobbly PS1 feel
            vec2 jitter = vec2(
                sin(time * 30.0 + vUv.y * 100.0) * jitterAmount,
                cos(time * 25.0 + vUv.x * 80.0) * jitterAmount * 0.5
            );
            vec2 uv = vUv + jitter;

            vec3 color = texture2D(tDiffuse, uv).rgb;

            // Dithering
            vec2 pixelPos = uv * resolution;
            float dither = bayer4(pixelPos) * ditherIntensity;

            // Color quantization (PS1 had 15-bit color = 32 levels per channel)
            color = floor((color + dither) * colorDepth) / colorDepth;

            // Slight darkening in corners (fake vertex lighting feel)
            float corner = 1.0 - length(vUv - 0.5) * 0.15;
            color *= corner;

            gl_FragColor = vec4(color, 1.0);
        }
    `,
};

// =============================================================================
// VHS Shader - Noise, tracking lines, color bleed
// =============================================================================

export const VHSShader = {
    uniforms: {
        tDiffuse: { value: null },
        time: { value: 0 },
        noiseIntensity: { value: 0.08 },
        colorBleed: { value: 0.005 },
        trackingIntensity: { value: 0.02 },
        scanDistort: { value: 0.002 },
    },

    vertexShader: /* glsl */ `
        varying vec2 vUv;
        void main() {
            vUv = uv;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
    `,

    fragmentShader: /* glsl */ `
        uniform sampler2D tDiffuse;
        uniform float time;
        uniform float noiseIntensity;
        uniform float colorBleed;
        uniform float trackingIntensity;
        uniform float scanDistort;

        varying vec2 vUv;

        // Pseudo-random noise
        float hash(vec2 p) {
            return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
        }

        float noise(vec2 p) {
            vec2 i = floor(p);
            vec2 f = fract(p);
            f = f * f * (3.0 - 2.0 * f);

            float a = hash(i);
            float b = hash(i + vec2(1.0, 0.0));
            float c = hash(i + vec2(0.0, 1.0));
            float d = hash(i + vec2(1.0, 1.0));

            return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
        }

        void main() {
            vec2 uv = vUv;

            // Horizontal scan distortion (tracking issues)
            float scanY = floor(vUv.y * 480.0);
            float tracking = sin(scanY * 0.1 + time * 2.0) * trackingIntensity;
            tracking += sin(scanY * 0.02 + time * 0.5) * trackingIntensity * 2.0;

            // Random tracking glitches
            float glitchChance = noise(vec2(time * 0.1, scanY * 0.001));
            if (glitchChance > 0.995) {
                tracking += (noise(vec2(time * 10.0, scanY)) - 0.5) * 0.1;
            }

            uv.x += tracking;

            // Per-line slight offset (scan wobble)
            uv.x += sin(scanY + time * 60.0) * scanDistort;

            // Color bleed (horizontal smearing)
            float r = texture2D(tDiffuse, uv + vec2(colorBleed * 2.0, 0.0)).r;
            float g = texture2D(tDiffuse, uv + vec2(colorBleed, 0.0)).g;
            float b = texture2D(tDiffuse, uv).b;
            vec3 color = vec3(r, g, b);

            // Static noise
            float staticNoise = noise(vUv * 500.0 + time * 100.0);
            color += (staticNoise - 0.5) * noiseIntensity;

            // Occasional horizontal noise band
            float bandNoise = noise(vec2(time * 0.5, 0.0));
            float bandY = fract(bandNoise * 10.0);
            float bandDist = abs(vUv.y - bandY);
            if (bandDist < 0.02) {
                float bandIntensity = (1.0 - bandDist / 0.02) * 0.3;
                color += (noise(vec2(vUv.x * 100.0, time * 50.0)) - 0.5) * bandIntensity;
            }

            // Slight desaturation (worn tape look)
            float gray = dot(color, vec3(0.299, 0.587, 0.114));
            color = mix(vec3(gray), color, 0.85);

            // Vignette
            float vignette = 1.0 - length(vUv - 0.5) * 0.4;
            color *= vignette;

            gl_FragColor = vec4(color, 1.0);
        }
    `,
};

// =============================================================================
// Noir Shader - High contrast, desaturated, film grain
// =============================================================================

export const NoirShader = {
    uniforms: {
        tDiffuse: { value: null },
        time: { value: 0 },
        contrast: { value: 1.4 },
        saturation: { value: 0.15 },
        grainIntensity: { value: 0.08 },
        vignetteIntensity: { value: 0.5 },
    },

    vertexShader: /* glsl */ `
        varying vec2 vUv;
        void main() {
            vUv = uv;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
    `,

    fragmentShader: /* glsl */ `
        uniform sampler2D tDiffuse;
        uniform float time;
        uniform float contrast;
        uniform float saturation;
        uniform float grainIntensity;
        uniform float vignetteIntensity;

        varying vec2 vUv;

        float hash(vec2 p) {
            return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
        }

        void main() {
            vec3 color = texture2D(tDiffuse, vUv).rgb;

            // Desaturate
            float gray = dot(color, vec3(0.299, 0.587, 0.114));
            color = mix(vec3(gray), color, saturation);

            // Increase contrast
            color = (color - 0.5) * contrast + 0.5;

            // Film grain
            float grain = hash(vUv * 1000.0 + time * 100.0);
            grain = (grain - 0.5) * grainIntensity;
            color += grain;

            // Heavy vignette for that noir spotlight feel
            vec2 vignetteUv = vUv - 0.5;
            float vignette = 1.0 - dot(vignetteUv, vignetteUv) * vignetteIntensity * 3.0;
            vignette = smoothstep(0.0, 1.0, vignette);
            color *= vignette;

            // Clamp to valid range
            color = clamp(color, 0.0, 1.0);

            gl_FragColor = vec4(color, 1.0);
        }
    `,
};

// =============================================================================
// Pixelate Shader - Low resolution effect
// =============================================================================

export const PixelateShader = {
    uniforms: {
        tDiffuse: { value: null },
        resolution: { value: null },
        pixelSize: { value: 4.0 },
    },

    vertexShader: /* glsl */ `
        varying vec2 vUv;
        void main() {
            vUv = uv;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
    `,

    fragmentShader: /* glsl */ `
        uniform sampler2D tDiffuse;
        uniform vec2 resolution;
        uniform float pixelSize;

        varying vec2 vUv;

        void main() {
            vec2 pixelatedUv = floor(vUv * resolution / pixelSize) * pixelSize / resolution;
            gl_FragColor = texture2D(tDiffuse, pixelatedUv);
        }
    `,
};
