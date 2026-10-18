<?php
declare(strict_types=1);

require dirname(__DIR__) . '/includes/feed.php';

$host = $_SERVER['HTTP_HOST'] ?? '127.0.0.1';
if (str_starts_with($host, '[')) {
    $end = strpos($host, ']');
    $name = $end === false ? '127.0.0.1' : substr($host, 1, $end - 1);
    $shown = '[' . $name . ']';
} else {
    $name = preg_replace('/:\d+$/', '', $host);
    $shown = is_string($name) && $name !== '' ? $name : '127.0.0.1';
}
$channels = [];
foreach (['A' => 'inputA', 'B' => 'inputB'] as $id => $path) {
    $channels[$id] = [
        'path' => $path,
        'whep' => 'http://' . $shown . ':8889/' . $path . '/whep',
    ];
}
nexencoder_json(200, ['ok' => true, 'channels' => $channels]);
