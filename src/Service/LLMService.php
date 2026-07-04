<?php

namespace App\Service;

use Symfony\Contracts\Cache\CacheInterface;
use Symfony\Contracts\Cache\ItemInterface;
use Symfony\Contracts\HttpClient\Exception\TransportExceptionInterface;
use Symfony\Contracts\HttpClient\HttpClientInterface;
use Twig\Environment;

class LLMService implements LLMServiceInterface
{
    private const CACHE_KEY_PREFIX = 'llm_models_';
    private const CACHE_TTL = 3600;

    /**
     * @param array<string, array{url: string, key: string|null}> $providers
     */
    public function __construct(
        private readonly HttpClientInterface $client,
        private readonly CacheInterface $cache,
        private readonly Environment $twig,
        private readonly array $providers,
        private readonly float $temperature = 1.0,
        private readonly int $timeout = 120,
    ) {
    }

    public function getAvailableModels(): array
    {
        $allModels = [];
        foreach ($this->providers as $providerName => $config) {
            $models = $this->cache->get(
                self::CACHE_KEY_PREFIX . $providerName,
                function (ItemInterface $item) use ($providerName, $config): array {
                    $item->expiresAfter(self::CACHE_TTL);
                    return $this->fetchModelsFromProvider($providerName, $config);
                }
            );
            array_push($allModels, ...$models);
        }
        return $allModels;
    }

    public function generateText(string $model, string $prompt): string
    {
        [$providerName, $modelName] = $this->parseModel($model);

        $config = $this->providers[$providerName];
        $finalPrompt = $this->twig->render('prompt.twig', ['prompt' => $prompt]);

        try {
            $response = $this->client->request('POST', $this->endpoint($config['url'], 'chat/completions'), [
                'headers' => $this->buildHeaders($config),
                'json' => [
                    'model' => $modelName,
                    'messages' => [['role' => 'user', 'content' => $finalPrompt]],
                    'stream' => false,
                    'temperature' => $this->temperature,
                ],
                // 'timeout' is the idle timeout (max wait for a response chunk). With
                // stream=false, reasoning models emit nothing until generation finishes,
                // so this must cover the whole generation, not just connect time.
                // 'max_duration' is the hard ceiling on the request as a whole.
                'timeout' => $this->timeout,
                'max_duration' => $this->timeout,
            ]);

            // request() is lazy; the network I/O (and any transport/idle-timeout
            // exception) happens here, so status handling stays inside the try.
            $statusCode = $response->getStatusCode();
            if ($statusCode !== 200) {
                throw new \RuntimeException(sprintf(
                    'Provider "%s" returned HTTP %d during text generation: %s',
                    $providerName,
                    $statusCode,
                    mb_substr($response->getContent(false), 0, 500)
                ));
            }

            $data = $response->toArray();
        } catch (TransportExceptionInterface $e) {
            throw new \RuntimeException(sprintf('Provider "%s" is unreachable: %s', $providerName, $e->getMessage()), 0, $e);
        }

        return $data['choices'][0]['message']['content'] ?? '';
    }

    /**
     * @return array{string, string} [providerName, modelName]
     */
    private function parseModel(string $model): array
    {
        $slashPos = strpos($model, '/');
        if ($slashPos === false) {
            throw new \InvalidArgumentException(sprintf('Model "%s" must be prefixed with a provider name (e.g. "my-provider/model-name").', $model));
        }

        $providerName = substr($model, 0, $slashPos);
        $modelName = substr($model, $slashPos + 1);

        if (!isset($this->providers[$providerName])) {
            throw new \InvalidArgumentException(sprintf('Unknown provider "%s". Check your llm_providers.yaml configuration.', $providerName));
        }

        return [$providerName, $modelName];
    }

    /**
     * @param array{url: string, key: string|null} $config
     * @return array<int, array{name: string}>
     */
    private function fetchModelsFromProvider(string $providerName, array $config): array
    {
        try {
            $response = $this->client->request('GET', $this->endpoint($config['url'], 'models'), [
                'headers' => $this->buildHeaders($config),
                'timeout' => 5,
                'max_duration' => 10,
            ]);
        } catch (TransportExceptionInterface) {
            return [];
        }

        if ($response->getStatusCode() !== 200) {
            return [];
        }

        $data = $response->toArray();
        $models = [];
        foreach ($data['data'] ?? [] as $model) {
            if (isset($model['id'])) {
                $models[] = ['name' => $providerName . '/' . $model['id']];
            }
        }
        return $models;
    }

    /**
     * Builds an OpenAI-compatible endpoint URL, tolerating base URLs that already
     * include a trailing "/v1" or "/" so both "https://host" and "https://host/v1"
     * resolve to the same ".../v1/{path}".
     */
    private function endpoint(string $baseUrl, string $path): string
    {
        $base = rtrim($baseUrl, '/');
        if (str_ends_with($base, '/v1')) {
            $base = substr($base, 0, -3);
        }
        return $base . '/v1/' . $path;
    }

    /**
     * @param array{url: string, key: string|null} $config
     * @return array<string, string>
     */
    private function buildHeaders(array $config): array
    {
        $headers = ['Content-Type' => 'application/json', 'Accept' => 'application/json'];
        if (!empty($config['key'])) {
            $headers['Authorization'] = 'Bearer ' . $config['key'];
        }
        return $headers;
    }
}
