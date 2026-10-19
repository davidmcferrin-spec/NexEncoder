<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

$body = nexencoder_admin_request();
$action = $_GET['action'] ?? '';
$actions = [
    'save' => '/api/vendors/save',
    'start' => '/api/vendors/start',
    'stop' => '/api/vendors/stop',
    'remove' => '/api/vendors/remove',
    'pull' => '/api/vendors/pull',
    'load' => '/api/vendors/load',
];
if (!is_string($action) || !isset($actions[$action])) {
    nexencoder_json(400, ['ok' => false, 'detail' => 'vendor action is not valid']);
}
[$status, $payload] = nexencoder_feed($actions[$action], $body, [
    'Content-Type: application/json',
    'X-Requested-With: NexEncoder',
    'Content-Length: ' . strlen($body),
], 180);
nexencoder_relay($status, $payload);
