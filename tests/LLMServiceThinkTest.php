<?php

namespace App\Tests;

use App\Service\LLMService;
use Symfony\Bundle\FrameworkBundle\Test\KernelTestCase;
use Symfony\Component\Cache\Adapter\ArrayAdapter;
use Symfony\Component\HttpClient\MockHttpClient;
use Symfony\Component\HttpClient\Response\MockResponse;
use Twig\Environment;

/**
 * Pins the outgoing generation payload. `think` is an Ollama extension rather
 * than part of the OpenAI chat-completions spec, so it must only appear when
 * app.llm_think is set — a provider that speaks the spec strictly can reject a
 * request carrying an unknown field.
 */
class LLMServiceThinkTest extends KernelTestCase
{
    private const PROVIDERS = ['acme' => ['url' => 'https://provider.example/v1', 'key' => null]];

    /**
     * Runs a generation against a mock transport and returns the decoded body
     * that would have been sent to the provider.
     */
    private function capturePayload(?bool $think): array
    {
        self::bootKernel();
        $twig = static::getContainer()->get(Environment::class);

        $sent = null;
        $client = new MockHttpClient(function (string $method, string $url, array $options) use (&$sent) {
            $sent = json_decode($options['body'], true);

            return new MockResponse(
                json_encode(['choices' => [['message' => ['content' => ' and so it went.']]]]),
                ['response_headers' => ['content-type' => 'application/json']]
            );
        });

        $service = new LLMService($client, new ArrayAdapter(), $twig, self::PROVIDERS, 1.0, 120, $think);
        $service->generateText('acme/some-model', 'The road went on');

        return $sent;
    }

    public function test_01_thinkIsOmittedWhenUnset(): void
    {
        $payload = $this->capturePayload(null);

        $this->assertArrayNotHasKey('think', $payload);
        // The rest of the payload must still be intact.
        $this->assertSame('some-model', $payload['model']);
        $this->assertFalse($payload['stream']);
        $this->assertStringContainsString('The road went on', $payload['messages'][0]['content']);
    }

    public function test_02_thinkIsSentWhenEnabled(): void
    {
        $payload = $this->capturePayload(true);

        $this->assertArrayHasKey('think', $payload);
        $this->assertTrue($payload['think']);
    }

    public function test_03_thinkIsSentWhenExplicitlyDisabled(): void
    {
        // false is meaningfully different from null: it asks a reasoning model
        // not to think, rather than leaving the provider to its default.
        $payload = $this->capturePayload(false);

        $this->assertArrayHasKey('think', $payload);
        $this->assertFalse($payload['think']);
    }
}
