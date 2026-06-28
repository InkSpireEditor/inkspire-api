<?php

namespace App\Tests;

use App\Entity\User;
use App\Entity\Dir;
use App\Entity\File;
use Symfony\Component\HttpFoundation\Response;

class FilesControllerCreateTest extends AuthenticatedWebTestCase
{
    public function test_01_fileAndDirCreation(): void
    {
        // Test directory creation
        $this->client->jsonRequest('POST', '/api/dir', [
            'name' => 'My Test Directory',
            'summary' => 'Awesome summary',
        ]);
        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_CREATED);
        $dirResponse = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertArrayHasKey('id', $dirResponse);
        $this->assertEquals('My Test Directory', $dirResponse['name']);
        $this->assertEquals('Awesome summary', $dirResponse['summary']);

        $dirId = $dirResponse['id'];

        // Verify directory was created in database
        $this->entityManager->clear();
        $dirRepository = $this->entityManager->getRepository(Dir::class);
        $dir = $dirRepository->find($dirId);
        $this->assertNotNull($dir);
        $this->assertEquals('My Test Directory', $dir->getName());
        $this->assertEquals('Awesome summary', $dir->getSummary());
        $this->assertEquals($this->email, $dir->getUser()->getEmail());

        // Test file creation
        $this->client->jsonRequest('POST', '/api/file', [
            'name' => 'My Test File',
        ]);
        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_CREATED);
        $fileResponse = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertArrayHasKey('id', $fileResponse);
        $this->assertEquals('My Test File', $fileResponse['name']);
        $this->assertEquals(null, $fileResponse['dir']);

        $fileId = $fileResponse['id'];

        // Verify file was created in database
        $this->entityManager->clear();
        $fileRepository = $this->entityManager->getRepository(File::class);
        $file = $fileRepository->find($fileId);
        $this->assertNotNull($file);
        $this->assertEquals('My Test File', $file->getName());
        $this->assertMatchesRegularExpression('/my-test-file-[0-9a-f]{12}\.ink$/', $file->getPath());
        $this->assertFileExists($this->resolveFilePath($file->getPath()));
        $this->assertNull($file->getDir());
        $this->assertEquals($this->email, $file->getUser()->getEmail());

        // Test file creation with directory
        $this->client->jsonRequest('POST', '/api/file', [
            'name' => 'My Other Test File',
            'dir' => $dirId,
        ]);
        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_CREATED);
        $fileResponse = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertArrayHasKey('id', $fileResponse);
        $this->assertEquals('My Other Test File', $fileResponse['name']);
        $this->assertEquals($dirId, $fileResponse['dir']);

        $fileWithDirId = $fileResponse['id'];

        // Verify file with directory was created in database
        $this->entityManager->clear();
        $fileRepository = $this->entityManager->getRepository(File::class);
        $dirRepository = $this->entityManager->getRepository(Dir::class);
        $fileWithDir = $fileRepository->find($fileWithDirId);
        $this->assertNotNull($fileWithDir);
        $this->assertEquals('My Other Test File', $fileWithDir->getName());
        $this->assertMatchesRegularExpression('/my-other-test-file-[0-9a-f]{12}\.ink$/', $fileWithDir->getPath());
        $this->assertFileExists($this->resolveFilePath($fileWithDir->getPath()));
        $this->assertNotNull($fileWithDir->getDir());
        $this->assertEquals($dirId, $fileWithDir->getDir()->getId());
        $this->assertEquals($this->email, $fileWithDir->getUser()->getEmail());
    }

    public function test_02_creationDuplicateName(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file1 = new File();
        $file1->setUser($user);
        $file1->setName('Duplicate Name');
        $filename1 = $this->filePathGenerator->generate('Duplicate Name');
        $file1->setPath($filename1);
        file_put_contents($this->resolveFilePath($filename1), '');
        $this->entityManager->persist($file1);

        $dir1 = new Dir();
        $dir1->setUser($user);
        $dir1->setName('Duplicate Name');
        $dir1->setSummary('');
        $this->entityManager->persist($dir1);
        $this->entityManager->flush();

        $this->client->jsonRequest('POST', '/api/file', ['name' => 'Duplicate Name']);
        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_CREATED);
        $response = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertEquals('Duplicate Name (1)', $response['name']);

        $createdFileId = $response['id'];

        // Verify file was created with auto-renamed name in database
        $this->entityManager->clear();
        $fileRepository = $this->entityManager->getRepository(File::class);
        $createdFile = $fileRepository->find($createdFileId);
        $this->assertNotNull($createdFile);
        $this->assertEquals('Duplicate Name (1)', $createdFile->getName());
        $this->assertMatchesRegularExpression('/duplicate-name-1-[0-9a-f]{12}\.ink$/', $createdFile->getPath());
        $this->assertFileExists($this->resolveFilePath($createdFile->getPath()));
        $this->assertEquals($this->email, $createdFile->getUser()->getEmail());

        $this->client->jsonRequest('POST', '/api/dir', ['name' => 'Duplicate Name']);
        $this->assertResponseIsSuccessful();
        $this->assertResponseStatusCodeSame(Response::HTTP_CREATED);
        $response = json_decode($this->client->getResponse()->getContent(), true);
        $this->assertEquals('Duplicate Name (1)', $response['name']);

        $createdDirId = $response['id'];

        // Verify directory was created with auto-renamed name in database
        $this->entityManager->clear();
        $dirRepository = $this->entityManager->getRepository(Dir::class);
        $createdDir = $dirRepository->find($createdDirId);
        $this->assertNotNull($createdDir);
        $this->assertEquals('Duplicate Name (1)', $createdDir->getName());
        $this->assertEquals($this->email, $createdDir->getUser()->getEmail());
    }

    public function test_03_uniqueConstraintEnforcedAtDbLevel(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $file1 = new File();
        $file1->setUser($user);
        $file1->setName('Race Condition File');
        $file1->setPath($this->filePathGenerator->generate('Race Condition File')); // filename only
        $this->entityManager->persist($file1);
        $this->entityManager->flush();

        // Bypass the soft-uniqueness check (as a concurrent request would) and insert directly
        $file2 = new File();
        $file2->setUser($user);
        $file2->setName('Race Condition File');
        $file2->setPath($this->filePathGenerator->generate('Race Condition File (1)')); // filename only
        $this->entityManager->persist($file2);

        $this->expectException(\Doctrine\DBAL\Exception\UniqueConstraintViolationException::class);
        $this->entityManager->flush();
    }

    public function test_04_uniqueConstraintDirEnforcedAtDbLevel(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user = $userRepository->findOneBy(['email' => $this->email]);

        $dir1 = new Dir();
        $dir1->setUser($user);
        $dir1->setName('Race Condition Dir');
        $this->entityManager->persist($dir1);
        $this->entityManager->flush();

        $dir2 = new Dir();
        $dir2->setUser($user);
        $dir2->setName('Race Condition Dir');
        $this->entityManager->persist($dir2);

        $this->expectException(\Doctrine\DBAL\Exception\UniqueConstraintViolationException::class);
        $this->entityManager->flush();
    }

    public function test_05_fileCreateInvalidName(): void
    {
        // Empty name → 422
        $this->client->jsonRequest('POST', '/api/file', ['name' => '']);
        $this->assertResponseStatusCodeSame(Response::HTTP_UNPROCESSABLE_ENTITY);

        // Whitespace-only name → 422
        $this->client->jsonRequest('POST', '/api/file', ['name' => '   ']);
        $this->assertResponseStatusCodeSame(Response::HTTP_UNPROCESSABLE_ENTITY);

        // Name too long → 422
        $this->client->jsonRequest('POST', '/api/file', ['name' => str_repeat('a', 256)]);
        $this->assertResponseStatusCodeSame(Response::HTTP_UNPROCESSABLE_ENTITY);
    }

    public function test_06_dirCreateInvalidNameOrSummary(): void
    {
        // Empty name → 422
        $this->client->jsonRequest('POST', '/api/dir', ['name' => '']);
        $this->assertResponseStatusCodeSame(Response::HTTP_UNPROCESSABLE_ENTITY);

        // Summary too long → 422
        $this->client->jsonRequest('POST', '/api/dir', [
            'name' => 'Valid Name',
            'summary' => str_repeat('x', 2001),
        ]);
        $this->assertResponseStatusCodeSame(Response::HTTP_UNPROCESSABLE_ENTITY);
    }

    public function test_07_fileCreateWithInvalidDir(): void
    {
        $userRepository = $this->entityManager->getRepository(User::class);
        $user1 = $userRepository->findOneBy(['email' => $this->email]);

        // Create user2 and their directory
        $user2 = $this->createUser('user2-dir@example.com', 'password');
        $this->entityManager->persist($user2);

        $user2Dir = new Dir();
        $user2Dir->setUser($user2);
        $user2Dir->setName('User2 Directory');
        $this->entityManager->persist($user2Dir);
        $this->entityManager->flush();

        // User1 tries to create a file in user2's directory
        $this->client->jsonRequest('POST', '/api/file', [
            'name' => 'Sneaky File',
            'dir' => $user2Dir->getId(),
        ]);
        $this->assertResponseStatusCodeSame(Response::HTTP_FORBIDDEN);

        // Verify no file was created with this name in database
        $this->entityManager->clear();
        $fileRepository = $this->entityManager->getRepository(File::class);
        $sneakyFile = $fileRepository->findOneBy(['name' => 'Sneaky File']);
        $this->assertNull($sneakyFile);
        // Assert no sneaky-file-*.ink was created on disk
        $container = static::getContainer();
        $filesDir = $container->getParameter('kernel.project_dir') . '/' . $container->getParameter('app.files_dir');
        $this->assertEmpty(glob($filesDir . '/sneaky-file-*.ink'));
    }
}
