<?php

namespace App\Tests;

use App\Entity\User;
use App\Entity\File;
use App\Service\OllamaServiceInterface;
use App\Tests\Mock\MockOllamaService;
use Symfony\Component\HttpFoundation\Response;

class OllamaControllerTest extends AuthenticatedWebTestCase
{
    private MockOllamaService $mockOllamaService;

    protected function setUp(): void
    {
        parent::setUp();

        // Disable kernel reboot between requests so the container (and mock instance) is stable
        $this->client->disableReboot();

        $this->mockOllamaService = static::getContainer()->get(OllamaServiceInterface::class);

        $this->assertInstanceOf(MockOllamaService::class, $this->mockOllamaService,
            'OllamaServiceInterface should be mocked with MockOllamaService');

        $this->mockOllamaService->reset();
    }

    protected function tearDown(): void
    {
        // Reset le mock après chaque test
        $this->mockOllamaService->reset();

        parent::tearDown();
    }

    public function test_01_generate_success(): void
    {
        $generatedText = 'This is the AI generated text.';
        $this->mockOllamaService->setReturnedText($generatedText);

        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('Ollama Test File');
        $filename = $this->filePathGenerator->generate('Ollama Test File');
        $file->setPath($filename);
        $initialContent = 'Initial content. ';
        $absolutePath = $this->resolveFilePath($filename);
        file_put_contents($absolutePath, $initialContent);

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $this->client->jsonRequest('POST', '/api/ollama/generate', [
            'id' => $file->getId(),
            'model' => 'test-model',
            'prompt' => 'Write a story'
        ]);

        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_OK);

        $data = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertArrayHasKey('snippet', $data);
        $this->assertEquals($generatedText, $data['snippet']);

        $updatedContent = file_get_contents($absolutePath);
        $this->assertEquals($initialContent . $generatedText, $updatedContent);
    }

    public function test_02_generate_missing_params(): void
    {
        $this->mockOllamaService->setReturnedText('dummy');

        $this->client->jsonRequest('POST', '/api/ollama/generate', [
            'id' => 1
            // missing model and prompt
        ]);

        $this->assertResponseStatusCodeSame(Response::HTTP_BAD_REQUEST);
    }

    public function test_03_generate_file_forbidden(): void
    {
        $this->mockOllamaService->setReturnedText('dummy');

        // Create a second user
        $user2 = $this->createUser('user2_ollama@example.com', 'password2');
        $this->entityManager->persist($user2);
        $this->entityManager->flush();

        // Create a file owned by that second user
        $foreignFile = new File();
        $foreignFile->setUser($user2);
        $foreignFile->setName('OtherUserFile.md');
        $filename = $this->filePathGenerator->generate('OtherUserFile.md');
        $foreignFile->setPath($filename);
        file_put_contents($this->resolveFilePath($filename), 'some content');

        $this->entityManager->persist($foreignFile);
        $this->entityManager->flush();

        // Attempt to generate using user1's credentials
        $this->client->jsonRequest('POST', '/api/ollama/generate', [
            'id' => $foreignFile->getId(),
            'model' => 'test-model',
            'prompt' => 'prompt'
        ]);

        $this->assertResponseStatusCodeSame(Response::HTTP_FORBIDDEN);
    }

    public function test_04_generate_unauthorized(): void
    {
        $this->mockOllamaService->setReturnedText('dummy');

        $this->deauthenticateClient();
        $this->client->jsonRequest('POST', '/api/ollama/generate', [
            'id' => 1,
            'model' => 'test-model',
            'prompt' => 'prompt'
        ]);

        $this->assertResponseStatusCodeSame(Response::HTTP_UNAUTHORIZED);
    }

    public function test_05_list_models_success(): void
    {
        $mockModels = [['name' => 'model1'], ['name' => 'model2']];
        $this->mockOllamaService->setAvailableModels($mockModels);

        $this->client->request('GET', '/api/ollama/models');

        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_OK);

        $data = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertEquals($mockModels, $data);
    }

    public function test_06_list_models_exception(): void
    {
        $this->mockOllamaService->setShouldThrowOnGetModels(true);

        $this->client->request('GET', '/api/ollama/models');

        $this->assertResponseStatusCodeSame(Response::HTTP_INTERNAL_SERVER_ERROR);
        $data = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertArrayHasKey('message', $data);
    }

    public function test_07_generate_invalid_inputs(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('Validation File');
        $filename = $this->filePathGenerator->generate('Validation File');
        $file->setPath($filename);
        file_put_contents($this->resolveFilePath($filename), '');
        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $id = $file->getId();

        // Model name too long → 422
        $this->client->jsonRequest('POST', '/api/ollama/generate', [
            'id' => $id,
            'model' => str_repeat('m', 256),
            'prompt' => 'Valid prompt',
        ]);
        $this->assertResponseStatusCodeSame(Response::HTTP_UNPROCESSABLE_ENTITY);

        // Prompt too long → 422
        $this->client->jsonRequest('POST', '/api/ollama/generate', [
            'id' => $id,
            'model' => 'valid-model',
            'prompt' => str_repeat('p', 10001),
        ]);
        $this->assertResponseStatusCodeSame(Response::HTTP_UNPROCESSABLE_ENTITY);
    }

    public function test_08_generate_path_escapes_storage_root(): void
    {
        $this->mockOllamaService->setReturnedText('should not reach here');

        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('Tampered Ollama File');
        $file->setPath('/tmp/ollama-escape.txt');

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $this->client->catchExceptions(true);
        $this->client->jsonRequest('POST', '/api/ollama/generate', [
            'id' => $file->getId(),
            'model' => 'test-model',
            'prompt' => 'Write a story',
        ]);

        $this->assertResponseStatusCodeSame(Response::HTTP_INTERNAL_SERVER_ERROR);
        $this->assertFileDoesNotExist('/tmp/ollama-escape.txt');
    }
}
