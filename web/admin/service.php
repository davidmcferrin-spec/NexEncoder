<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

nexencoder_admin_request();
$unit = $_GET['unit'] ?? '';
$action = $_GET['action'] ?? '';
if (!is_string($unit) || !is_string($action) || $unit === '' || $action === '') {
    nexencoder_json(400, ['ok' => false, 'detail' => 'unit and action are required']);
}
if (strlen($unit) > 200 || strlen($action) > 32 || str_contains($unit, "\0") || str_contains($action, "\0")) {
    nexencoder_json(400, ['ok' => false, 'detail' => 'unit or action is not valid']);
}
$path = '/api/services/' . rawurlencode($unit) . '?action=' . rawurlencode($action);
[$status, $payload] = nexencoder_feed($path, '', [
    'X-Requested-With: NexEncoder',
    'Content-Length: 0',
]);
nexencoder_relay($status, $payload);
