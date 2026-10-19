<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

if (($_SERVER['REQUEST_METHOD'] ?? '') === 'GET') {
    nexencoder_get_pass('/api/timezone');
}

$body = nexencoder_admin_request();
[$status, $payload] = nexencoder_feed('/api/timezone', $body, [
    'Content-Type: application/json',
    'X-Requested-With: NexEncoder',
    'Content-Length: ' . strlen($body),
]);
nexencoder_relay($status, $payload);
