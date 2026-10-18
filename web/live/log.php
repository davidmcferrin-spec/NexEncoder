<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

$unit = $_GET['unit'] ?? '';
if (!is_string($unit) || preg_match('/^[A-Za-z0-9@._-]+$/', $unit) !== 1 || strlen($unit) > 200) {
    nexencoder_json(400, ['ok' => false, 'detail' => 'unit is not valid']);
}
nexencoder_stream('/api/services/' . rawurlencode($unit) . '/log');
