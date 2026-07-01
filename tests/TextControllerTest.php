<?php

namespace App\Tests;

use App\Entity\User;
use App\Entity\File;
use Symfony\Component\HttpFoundation\Response;

class TextControllerTest extends AuthenticatedWebTestCase
{
    public function test_01_file_contents_success(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('Test File');
        $filename = $this->filePathGenerator->generate('Test File');
        $file->setPath($filename);
        $fileContent = 'This is the content of the test file.';
        file_put_contents($this->resolveFilePath($filename), $fileContent);

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $this->client->request('GET', '/api/file/' . $file->getId() . '/contents');

        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_OK);
        $this->assertEquals($fileContent, $this->client->getResponse()->getContent());
    }

    public function test_02_file_contents_forbidden(): void
    {
        // Create a second user
        $user2 = $this->createUser('user2@example.com', 'password2');
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

        // Attempt to get content using user1's credentials
        $this->client->request('GET', '/api/file/' . $foreignFile->getId() . '/contents');

        // Expect 403 Forbidden
        $this->assertResponseStatusCodeSame(Response::HTTP_FORBIDDEN);
    }

    public function test_03_file_contents_unauthorized(): void
    {
        $user = $this->createUser('unauth@example.com', 'pw');
        $this->entityManager->persist($user);

        $file = new File();
        $file->setUser($user);
        $file->setName('UnauthorizedFile.md');
        $filename = $this->filePathGenerator->generate('UnauthorizedFile.md');
        $file->setPath($filename);
        file_put_contents($this->resolveFilePath($filename), 'some content');

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $this->deauthenticateClient();
        $this->client->request('GET', '/api/file/' . $file->getId() . '/contents');

        $this->assertResponseStatusCodeSame(Response::HTTP_UNAUTHORIZED);
    }

    public function test_04_file_contents_not_found_on_disk(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('File not on disk');
        $filename = $this->filePathGenerator->generate('File not on disk');
        $file->setPath($filename);
        // We don't write the file to disk

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $this->client->request('GET', '/api/file/' . $file->getId() . '/contents');

        $this->assertResponseStatusCodeSame(Response::HTTP_NOT_FOUND);
        $this->assertFileDoesNotExist($this->resolveFilePath($filename));
    }

    public function test_05_update_file_contents_success(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('Test File to Update');
        $filename = $this->filePathGenerator->generate('Test File to Update');
        $file->setPath($filename);
        $initialContent = 'This is the initial content.';
        $absolutePath = $this->resolveFilePath($filename);
        file_put_contents($absolutePath, $initialContent);

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $newContent = 'This is the updated content.';
        $this->client->request('PUT', '/api/file/' .$file->getId() . '/contents', [], [], [], $newContent);

        $this->assertResponseStatusCodeSame(Response::HTTP_NO_CONTENT);

        $updatedContent = file_get_contents($absolutePath);
        $this->assertEquals($newContent, $updatedContent);
    }

    public function test_06_update_file_contents_forbidden(): void
    {
        // Create a second user
        $user2 = $this->createUser('user2_update@example.com', 'password2');
        $this->entityManager->persist($user2);
        $this->entityManager->flush();

        // Create a file owned by that second user
        $foreignFile = new File();
        $foreignFile->setUser($user2);
        $foreignFile->setName('OtherUserFileUpdate.md');
        $filename = $this->filePathGenerator->generate('OtherUserFileUpdate.md');
        $foreignFile->setPath($filename);
        $content = 'some content';
        $absolutePath = $this->resolveFilePath($filename);
        file_put_contents($absolutePath, $content);

        $this->entityManager->persist($foreignFile);
        $this->entityManager->flush();

        // Attempt to update content using user1's credentials
        $newContent = 'This is the updated content.';
        $this->client->request('PUT', '/api/file/' .$foreignFile->getId() . '/contents', [], [], [], $newContent);

        // Expect 403 Forbidden
        $this->assertResponseStatusCodeSame(Response::HTTP_FORBIDDEN);
        $updatedContent = file_get_contents($absolutePath);
        $this->assertEquals($content, $updatedContent);
        $this->assertNotEquals($newContent, $updatedContent);
    }

    public function test_07_update_file_contents_unauthorized(): void
    {
        $user = $this->createUser('unauth_update@example.com', 'pw');
        $this->entityManager->persist($user);

        $file = new File();
        $file->setUser($user);
        $file->setName('UnauthorizedFileUpdate.md');
        $filename = $this->filePathGenerator->generate('UnauthorizedFileUpdate.md');
        $file->setPath($filename);
        $content = 'some content';
        $absolutePath = $this->resolveFilePath($filename);
        file_put_contents($absolutePath, $content);

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $this->deauthenticateClient();
        $newContent = 'This is the updated content.';
        $this->client->request('PUT', '/api/file/' .$file->getId() . '/contents', [], [], [], $newContent);

        $this->assertResponseStatusCodeSame(Response::HTTP_UNAUTHORIZED);
        $updatedContent = file_get_contents($absolutePath);
        $this->assertEquals($content, $updatedContent);
        $this->assertNotEquals($newContent, $updatedContent);
    }

    public function test_08_update_file_contents_not_found_on_disk(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('File not on disk update');
        $filename = $this->filePathGenerator->generate('File not on disk update');
        $file->setPath($filename);
        // We don't write the file to disk

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $newContent = 'This is the updated content.';
        $this->client->request('PUT', '/api/file/' .$file->getId() . '/contents', [], [], [], $newContent);

        $this->assertResponseStatusCodeSame(Response::HTTP_NOT_FOUND);
        $this->assertFileDoesNotExist($this->resolveFilePath($filename));
    }

    public function test_09_file_contents_path_escapes_storage_root(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('Tampered File');
        $file->setPath('/etc/passwd');

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $this->client->catchExceptions(true);
        $this->client->request('GET', '/api/file/' . $file->getId() . '/contents');

        $this->assertResponseStatusCodeSame(Response::HTTP_INTERNAL_SERVER_ERROR);
    }

    public function test_10_update_file_contents_path_escapes_storage_root(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file = new File();
        $file->setUser($user);
        $file->setName('Tampered File Write');
        $file->setPath('/tmp/should-not-be-written.txt');

        $this->entityManager->persist($file);
        $this->entityManager->flush();

        $this->client->catchExceptions(true);
        $this->client->request('PUT', '/api/file/' .$file->getId() . '/contents', [], [], [], 'evil content');

        $this->assertResponseStatusCodeSame(Response::HTTP_INTERNAL_SERVER_ERROR);
        $this->assertFileDoesNotExist('/tmp/should-not-be-written.txt');
    }
}
