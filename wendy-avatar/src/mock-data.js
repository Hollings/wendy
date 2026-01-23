/**
 * mock-data.js - Real session data for testing
 *
 * This is actual data from Wendy's stream.jsonl, cleaned up for replay.
 * Session: Wendy fixes a platformer game hitbox
 */

export const MOCK_SESSION = [
    {
        delay: 0,
        event: {
            "type": "system",
            "subtype": "init",
            "cwd": "/data/wendy/coding",
            "session_id": "mock-session-001",
            "model": "claude-sonnet-4-5-20250929"
        }
    },
    {
        delay: 1500,
        event: {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_check_001",
                    "name": "Bash",
                    "input": {
                        "command": "curl -s http://localhost:8945/api/check_messages/1461429474250850365",
                        "description": "Check for new messages in channel"
                    }
                }]
            }
        }
    },
    {
        delay: 800,
        event: {
            "type": "user",
            "message": {
                "content": [{
                    "tool_use_id": "toolu_check_001",
                    "type": "tool_result",
                    "content": JSON.stringify({
                        "messages": [
                            {
                                "message_id": 1463953119145623684,
                                "author": "hollingsnot3",
                                "content": "remove the hitbox outside the green plane",
                                "timestamp": 1769104022
                            },
                            {
                                "message_id": 1463953137688903814,
                                "author": "hollingsnot3",
                                "content": "so i can fall below ground",
                                "timestamp": 1769104026
                            }
                        ],
                        "task_updates": []
                    }),
                    "is_error": false
                }]
            }
        }
    },
    {
        delay: 2000,
        event: {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_read_001",
                    "name": "Read",
                    "input": {
                        "file_path": "/data/wendy/coding/platformer3d/game.js",
                        "offset": 120,
                        "limit": 30
                    }
                }]
            }
        }
    },
    {
        delay: 500,
        event: {
            "type": "user",
            "message": {
                "content": [{
                    "tool_use_id": "toolu_read_001",
                    "type": "tool_result",
                    "content": `   120→function checkCollision(position, height) {
   121→    const playerBox = new THREE.Box3(
   122→        new THREE.Vector3(position.x - 0.5, position.y, position.z - 0.5),
   123→        new THREE.Vector3(position.x + 0.5, position.y + height, position.z + 0.5)
   124→    );
   125→
   126→    // Check ground
   127→    if (position.y <= 0) {
   128→        return { collision: true, normal: new THREE.Vector3(0, 1, 0), height: 0 };
   129→    }
   130→
   131→    // Check platforms
   132→    for (const platform of platforms) {`,
                    "is_error": false
                }]
            }
        }
    },
    {
        delay: 3000,
        event: {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_edit_001",
                    "name": "Edit",
                    "input": {
                        "file_path": "/data/wendy/coding/platformer3d/game.js",
                        "old_string": `    // Check ground
    if (position.y <= 0) {
        return { collision: true, normal: new THREE.Vector3(0, 1, 0), height: 0 };
    }`,
                        "new_string": `    // Check ground - only within the ground plane bounds
    const groundSize = 100;
    const halfGroundSize = groundSize / 2;

    if (position.y <= 0 &&
        position.x >= -halfGroundSize && position.x <= halfGroundSize &&
        position.z >= -halfGroundSize && position.z <= halfGroundSize) {
        return { collision: true, normal: new THREE.Vector3(0, 1, 0), height: 0 };
    }`
                    }
                }]
            }
        }
    },
    {
        delay: 500,
        event: {
            "type": "user",
            "message": {
                "content": [{
                    "tool_use_id": "toolu_edit_001",
                    "type": "tool_result",
                    "content": "The file /data/wendy/coding/platformer3d/game.js has been updated.",
                    "is_error": false
                }]
            }
        }
    },
    {
        delay: 2000,
        event: {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_deploy_001",
                    "name": "Bash",
                    "input": {
                        "command": "cd /data/wendy && ./deploy.sh coding/platformer3d",
                        "description": "Redeploy the platformer"
                    }
                }]
            }
        }
    },
    {
        delay: 1500,
        event: {
            "type": "user",
            "message": {
                "content": [{
                    "tool_use_id": "toolu_deploy_001",
                    "type": "tool_result",
                    "content": `Detected: site (found index.html)
Creating tarball of /data/wendy/coding/platformer3d...
Tarball size: 3083 bytes (~3KB)
Deploying 'coding/platformer3d' to wendy.monster/platformer3d/...
Deployment successful!

  Type:   site
  Source: your folder/coding/platformer3d/
  URL:    https://wendy.monster/platformer3d/`,
                    "is_error": false
                }]
            }
        }
    },
    {
        delay: 2500,
        event: {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_send_001",
                    "name": "Bash",
                    "input": {
                        "command": `curl -X POST http://localhost:8945/api/send_message -H 'Content-Type: application/json' -d '{"channel_id": "1461429474250850365", "content": "done - you can now fall off the edge into the void. ground collision only happens within the green plane bounds now"}'`,
                        "description": "Announce update"
                    }
                }]
            }
        }
    },
    {
        delay: 800,
        event: {
            "type": "user",
            "message": {
                "content": [{
                    "tool_use_id": "toolu_send_001",
                    "type": "tool_result",
                    "content": `{"success":true,"message":"Message queued"}`,
                    "is_error": false
                }]
            }
        }
    },
    {
        delay: 1500,
        event: {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "text",
                    "text": "No response requested."
                }]
            }
        }
    },
    {
        delay: 500,
        event: {
            "type": "result",
            "subtype": "success",
            "is_error": false,
            "duration_ms": 30585,
            "num_turns": 6,
            "result": "No response requested."
        }
    }
];

/**
 * A shorter session for quick testing - just check and reply
 */
export const MOCK_QUICK_SESSION = [
    {
        delay: 0,
        event: { "type": "system", "subtype": "init" }
    },
    {
        delay: 1000,
        event: {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Bash",
                    "input": { "command": "curl -s http://localhost:8945/api/check_messages/123" }
                }]
            }
        }
    },
    {
        delay: 500,
        event: {
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "content": JSON.stringify({
                        "messages": [{ "author": "TestUser", "content": "Hey Wendy!" }]
                    })
                }]
            }
        }
    },
    {
        delay: 2000,
        event: {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "text",
                    "text": "I'll respond to this friendly greeting!"
                }]
            }
        }
    },
    {
        delay: 1500,
        event: {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {
                        "command": `curl -X POST http://localhost:8945/api/send_message -d '{"content": "Hey! How can I help you today?"}'`
                    }
                }]
            }
        }
    },
    {
        delay: 500,
        event: {
            "type": "user",
            "message": {
                "content": [{ "type": "tool_result", "content": '{"success":true}' }]
            }
        }
    },
    {
        delay: 1000,
        event: { "type": "result", "is_error": false }
    }
];
