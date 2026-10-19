<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

nexencoder_admin_request();
$hours = $_GET['hours'] ?? '';
if (!is_string($hours) || !in_array($hours, ['24', '36', '48', '72', 'all'], true)) {
    nexencoder_json(400, ['ok' => false, 'detail' => 'hours must be 24, 36, 48, 72, or all']);
}
set_time_limit(180);
[$status, $type, $payload] = nexencoder_get('/api/support?hours=' . rawurlencode($hours), 150);
if ($status !== 200 || !str_contains($type, 'zip')) {
    nexencoder_pass($status, $type !== '' ? $type : 'application/json; charset=utf-8', $payload);
}
$stamp = gmdate('Ymd\THis\Z');
$label = $hours === 'all' ? 'all' : $hours . 'h';
header('Content-Type: application/zip');
header('Content-Disposition: attachment; filename="nexencoder-support-' . $stamp . '-' . $label . '.zip"');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
echo $payload;
